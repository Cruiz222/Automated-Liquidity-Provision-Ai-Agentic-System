"""Transactional paper execution with bounded event-time WebSocket reordering."""
import copy
from dataclasses import asdict, dataclass, field, replace
import threading

from .market_data import MarketEvent
from .okx_public import FeedError
from .paper import FeedState, PaperRunner


@dataclass
class StreamState(FeedState):
    epoch: int | None = None
    startup_ms: int | None = None
    buffer: list = field(default_factory=list)
    processed_book: MarketEvent | None = None
    last_book_sequence: int | None = None
    recent_ids: list = field(default_factory=list)
    resync_after_ms: int | None = None
    snapshot_resyncs: int = 0
    last_snapshot_resync_reason: str | None = None


class StreamPaperRunner(PaperRunner):
    def __init__(self, *args, reorder_ms=500, **kwargs):
        super().__init__(*args, **kwargs)
        if type(reorder_ms) is not int or not 0 <= reorder_ms < self.stale_ms:
            raise ValueError('reorder_ms must be a nonnegative integer below stale_ms')
        self.reorder_ms = reorder_ms
        self.feed = StreamState()

    def health(self):
        result = super().health()
        result.update(transport='OKX public WebSocket', connection_epoch=self.feed.epoch,
                      buffered_events=len(self.feed.buffer), reorder_ms=self.reorder_ms,
                      awaiting_snapshot=self.feed.processed_book is None,
                      snapshot_resyncs=self.feed.snapshot_resyncs,
                      last_snapshot_resync_reason=self.feed.last_snapshot_resync_reason,
                      buffered_books=sum(e['kind'] == 'book' for e in self.feed.buffer),
                      continuity='single-connection snapshots; no complete exchange sequence guarantee')
        if self.feed.processed_book is None:
            result['alignment_lag_ms'] = None
        return result

    def fail(self, error, raw=None):
        with self.lock:
            self.feed.buffer = []
            self.feed.processed_book = None
            self.feed.epoch = None
            self.feed.last_book_sequence = None
            self.feed.recent_ids = []
            self.feed.resync_after_ms = None
            super().fail(error, raw)

    def set_running(self, running):
        with self.lock:
            changed = self.running != running
            super().set_running(running)
            if changed and running:
                self.feed.buffer = []
                self.feed.processed_book = None
                self.feed.last_book_sequence = None

    def ingest(self, raw):
        with self.lock:
            if self.storage_error:
                raise RuntimeError(self.storage_error)
            try:
                return self._stream_ingest(raw)
            except (FeedError, ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
                self.fail(FeedError(f'WebSocket stream rejected: {error}'), raw)
                return False

    def _stream_ingest(self, raw):
        if raw['transport'] != 'websocket' or type(raw['epoch']) is not int or raw['epoch'] <= 0:
            raise FeedError('Invalid WebSocket connection epoch')
        received = self.clock()
        feed = copy.deepcopy(self.feed)
        session = copy.deepcopy(self.session, {id(self.session.account.lock): threading.RLock()})
        if feed.epoch != raw['epoch']:
            if feed.epoch is not None:
                feed.gaps += 1
            feed = StreamState(segment=feed.segment, gaps=feed.gaps, duplicates=feed.duplicates,
                               skipped_trades=feed.skipped_trades, epoch=raw['epoch'], startup_ms=received,
                               snapshot_resyncs=feed.snapshot_resyncs, last_snapshot_resync_reason=feed.last_snapshot_resync_reason)
            session.cancel_orders()
        if feed.received_ms is not None and received - feed.received_ms > self.stale_ms:
            raise FeedError('WebSocket ingestion stalled beyond freshness limit')
        for wrapper in raw['messages']:
            if type(wrapper['received_ms']) is not int or wrapper['received_ms'] > received + 1000:
                raise FeedError('Invalid message receipt timestamp')
            message = wrapper['payload']
            if message == 'pong':
                continue
            if not isinstance(message, dict):
                raise FeedError('Invalid WebSocket payload')
            if message.get('event'):
                if message['event'] != 'subscribe' or message.get('arg', {}).get('instId') != self.instrument or message.get('arg', {}).get('channel') not in ('books5','trades'):
                    raise FeedError('Subscription failed or unexpected control event')
                continue
            arg = message['arg']
            if arg['instId'] != self.instrument or arg['channel'] not in ('books5', 'trades'):
                raise FeedError('Unexpected stream instrument or channel')
            for row in message['data']:
                source_ms = int(row['ts'])
                if source_ms > received + 1000 or received - source_ms > self.stale_ms:
                    raise FeedError('Stale or future WebSocket event')
                if arg['channel'] == 'books5':
                    event = MarketEvent(source_ms, float(row['bids'][0][0]), float(row['asks'][0][0]),
                                        float(row['bids'][0][1]), float(row['asks'][0][1]))
                    sequence = int(row['seqId']) if 'seqId' in row else None
                    if sequence is not None and feed.last_book_sequence is not None:
                        if sequence < feed.last_book_sequence:
                            raise FeedError('Book sequence reset; resynchronization required')
                        if sequence == feed.last_book_sequence:
                            feed.duplicates += 1
                            continue
                    if feed.book and source_ms < feed.book.timestamp_ms:
                        raise FeedError('Book source timestamp regressed')
                    feed.book, feed.last_book_sequence = event, sequence
                    item = {'kind': 'book', 'source_ms': source_ms, 'id': sequence or 0, 'market': asdict(event)}
                else:
                    identifier = int(row['tradeId'])
                    if identifier < 0:
                        raise FeedError('Invalid trade ID')
                    if identifier in feed.recent_ids:
                        feed.duplicates += 1
                        continue
                    if feed.anchor is not None and identifier <= feed.anchor:
                        raise FeedError('Trade ID regressed outside deduplication window')
                    feed.anchor = identifier
                    feed.recent_ids = (feed.recent_ids + [identifier])[-4096:]
                    # Validate price, size and aggressor side before buffering.
                    event = MarketEvent(source_ms, 1, 2, 0, 0, float(row['px']), float(row['sz']), row['side'])
                    item = {'kind': 'trade', 'source_ms': source_ms, 'id': identifier,
                            'trade_price': event.trade_price, 'trade_size': event.trade_size, 'trade_side': event.trade_side}
                feed.buffer.append(item)
        if feed.book is None and received - feed.startup_ms > self.stale_ms:
            raise FeedError('No initial WebSocket book within freshness limit')
        if len(feed.buffer) > 5000:
            raise FeedError('Reorder buffer capacity exceeded')
        if feed.book is not None and received - feed.book.timestamp_ms > self.stale_ms:
            raise FeedError('WebSocket book freshness limit exceeded')
        cutoff = received - self.reorder_ms
        ready = sorted((e for e in feed.buffer if e['source_ms'] <= cutoff),
                       key=lambda e: (e['source_ms'], 0 if e['kind'] == 'book' else 1, e['id']))
        feed.buffer = [e for e in feed.buffer if e['source_ms'] > cutoff]
        records, skipped, resynchronizations = [], [], []

        def require_snapshot(source_ms, reason):
            session.cancel_orders()
            feed.processed_book = None
            feed.resync_after_ms = source_ms
            feed.snapshot_resyncs += 1
            feed.gaps += 1
            feed.last_snapshot_resync_reason = reason
            resynchronizations.append({'source_ms': source_ms, 'reason': reason})

        for item in ready:
            if item['kind'] == 'trade' and (feed.processed_book is None or item['source_ms'] < feed.segment_start_ms):
                feed.skipped_trades += 1
                skipped.append({'trade_id': item['id'], 'source_ms': item['source_ms'], 'reason': 'awaiting_fresh_snapshot'})
                continue
            if item['kind'] == 'book' and feed.processed_book is None and feed.resync_after_ms is not None and item['source_ms'] < feed.resync_after_ms:
                continue
            if session.previous is not None and item['source_ms'] < session.previous.timestamp_ms:
                raise FeedError('Event arrived after the reorder deadline')
            if item['kind'] == 'book':
                event = MarketEvent(**item['market'])
                if feed.processed_book is not None and event.timestamp_ms - feed.processed_book.timestamp_ms > self.stale_ms:
                    require_snapshot(event.timestamp_ms, 'Book updates resumed after a source freshness gap')
                if feed.processed_book is None:
                    feed.segment += 1
                    feed.segment_start_ms = event.timestamp_ms
                    feed.resync_after_ms = None
                    session.cancel_orders()
                feed.processed_book = event
            else:
                if item['source_ms'] - feed.processed_book.timestamp_ms > self.stale_ms:
                    require_snapshot(item['source_ms'], 'Trade has no fresh earlier book')
                    feed.skipped_trades += 1
                    skipped.append({'trade_id': item['id'], 'source_ms': item['source_ms'], 'reason': 'no_fresh_earlier_book'})
                    continue
                event = replace(feed.processed_book, timestamp_ms=item['source_ms'],
                                trade_price=item['trade_price'], trade_size=item['trade_size'], trade_side=item['trade_side'])
            session.step(event, submit=self.running, trade=self.running, decision_time_ms=received)
            records.append({'segment': feed.segment, 'market': asdict(event), 'execution': dict(session.last_execution),
                'decision': session.decisions[-1] if session.decisions and session.decisions[-1]['event'] == session.index else None,
                'fills': [f for f in session.fills if f['event'] == session.index],
                'account': {'cash': session.account.cash, 'inventory': session.account.inventory,
                            'equity': session.curve[-1]['equity'], 'fees': session.fees},
                'active_orders': copy.deepcopy(session.active), 'pending_orders': copy.deepcopy(session.pending), 'running': self.running})
        # Cancel as soon as received-but-buffered data proves the previous
        # snapshot can no longer support execution. Do not use a future book
        # to manufacture a fill for the preceding stale trade.
        probe = feed.processed_book
        if probe is not None:
            for item in sorted(feed.buffer, key=lambda e: (e['source_ms'], 0 if e['kind'] == 'book' else 1, e['id'])):
                if item['kind'] == 'book':
                    if item['source_ms'] - probe.timestamp_ms > self.stale_ms:
                        require_snapshot(item['source_ms'], 'Book updates resumed after a source freshness gap')
                        break
                    probe = MarketEvent(**item['market'])
                elif item['source_ms'] - probe.timestamp_ms > self.stale_ms:
                    require_snapshot(item['source_ms'], 'Trade has no fresh earlier book')
                    feed.skipped_trades += 1
                    skipped.append({'trade_id': item['id'], 'source_ms': item['source_ms'], 'reason': 'no_fresh_earlier_book'})
                    feed.buffer.remove(item)
                    break
        feed.status = 'healthy' if feed.processed_book is not None else 'synchronizing'
        feed.received_ms, feed.error = received, None
        saved_health = asdict(feed)
        saved_health.pop('buffer', None)
        saved_health.pop('recent_ids', None)
        saved_health['buffered_events'] = len(feed.buffer)
        self._append({'received_ms': received, 'raw': raw, 'events': records, 'health': saved_health, 'control': None,
                      'skipped_events': skipped, 'snapshot_resynchronizations': resynchronizations})
        self.feed, self.session = feed, session
        return True
