# NGN payouts without typing a bank code

## What the customer sees

1. Enter an **account number** (10 digits). No bank code.
2. The app lists the banks that could hold that number and, next to each, the **account name** returned by a name
   enquiry. The customer confirms the one that is theirs (or the app selects it when only one bank returns a name).
3. Save it as a payout account. From then on withdrawals just pick the saved account; the bank code is stored with it
   and never typed again.

The API accepts a withdrawal with `destination: {"account_number": "..."}` and no `bank_code`. If the bank cannot be
confirmed uniquely it answers `409 ambiguous_bank` with the candidates, and the customer picks a bank by name.

## Why a bank is still needed underneath

Every payout rail identifies the destination by bank plus account number, so the code is always sent; the change is
that the platform finds it instead of the customer. Two facts shape the design, both measured in the tests:

* The NUBAN check digit (CBN standard, implemented in `app/payouts/nuban.py` and verified against the CBN's worked
  examples) catches **any single-digit typo when the bank is known**, so a given bank code is always checked.
* It cannot identify the bank. With the starter directory, a random account number validates for about two banks, and
  every number validates for at least one. So when the bank is not given, **only the name enquiry can confirm it**.
  The code never silently picks a bank from the check digit alone.

## Simulation versus production

* `SimulatedNameEnquiry` invents names (prefixed `SIMULATED:`) and designates one candidate bank as the owner. It
  exists so the flow can be exercised. Replace it with a payout provider's account-name lookup (implement
  `NameEnquiry.lookup(bank_code, account_number)`); that is what makes the answer authoritative.
* `app/payouts/nigerian_banks.json` is a **starter list** of 22 codes that agree across several published sources.
  Stanbic IBTC, Polaris, Parallex, Globus, microfinance banks and fintech wallets are left out because published lists
  disagree or could not be verified. Load your provider's official list (`DEX_BANKS=path.json`) before real use.
  Five- and six-digit institution codes are supported by the algorithm.
* Fintech wallets (such as phone-number accounts) may not follow NUBAN; for those, the customer can still supply the
  bank code explicitly, or you can add them to the directory.

## Limits that still apply

Withdrawal limits follow the customer's KYC tier (`app/accounts/kyc.py`, placeholders to review), a 2FA code is
required when the customer has 2FA enabled, and a customer may keep at most 20 saved payout accounts.
