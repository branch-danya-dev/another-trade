# Live Operations and Compliance Gate

Status: **RESEARCH / PRE-LIVE GATE**

Last factual review: **2026-09-30**

This document is not part of the trading edge and does not change strategy v0.2.1. It exists because a technically valid bot is not deployable if account access, funding/withdrawal, residency rules, tax/reporting obligations, or exchange terms make live operation unreliable.

This is an engineering/compliance checklist, not legal or tax advice. Before real-money deployment, current rules must be re-verified and, where material, confirmed with a qualified Russian legal/tax professional.

## 1. Russian regulatory status as of 2026-09-30

Federal Law No. 282-FZ of 04.08.2026, "On Digital Currencies and Digital Rights", created a new comprehensive framework for digital-currency circulation in Russia. Core provisions entered into force on 2026-09-01.

Official / primary references:

- Bank of Russia crypto-market portal: https://www.cbr.ru/develop/cryptocurrency/
- Federal Law No. 282-FZ: https://www.consultant.ru/document/cons_doc_LAW_540983/
- Bank of Russia note on the law: https://www.cbr.ru/press/event/?id=32719

The regime distinguishes, among other things, regulated Russian intermediaries / crypto-exchange organizations, qualified and non-qualified investors, testing/disclosure requirements, and limits/eligible assets for some non-qualified-investor activity.

### Important transitional state

As of the latest Bank of Russia page reviewed on 2026-09-30, some implementing acts are still moving through registration / implementation.

For example, Bank of Russia Instruction No. 7441-U dated 2026-09-11, which addresses non-qualified-investor aggregate purchase limits and eligible digital currencies, is listed by the Bank of Russia as being under state registration with the Ministry of Justice, with possible changes before final registration.

Therefore:

- do not hard-code current public announcements about annual limits or eligible assets into the trading system;
- re-check the final registered acts immediately before any real-money launch;
- treat the regulatory state as a deployment dependency, not a static 2026 assumption.

## 2. Future reporting obligations

Amendments to Russian currency-control legislation introduce reporting by resident individuals on digital-currency operations.

The reviewed current text states that, from **2027-05-02**, resident individuals must submit reports on digital-currency operations to Russian tax authorities, except for resident individuals whose total stay outside Russia in the preceding calendar year exceeded 183 days.

Reference:
https://www.consultant.ru/document/cons_doc_LAW_540998/e07f3a5e4b089705af512b1d4058f49e1857300d/

Before live trading extends into this period, verify:

- whether the rule applies to the specific user's residency status;
- required report form and frequency;
- whether foreign-exchange account / wallet / exchange activity creates additional notifications;
- tax-basis and record-keeping requirements for derivatives, funding, fees, transfers, and crypto conversions.

The bot must retain complete transaction records suitable for later tax/accounting reconstruction regardless of whether reporting is currently required.

## 3. Bybit service availability

Current Bybit service-restriction page reviewed on 2026-09-30:

https://www.bybit.com/en/help-center/article/Service-Restricted-Countries

The page was last updated 2026-09-01.

At that snapshot, the Russian Federation as a whole is **not** listed as a generally excluded jurisdiction, while certain territories and sanctioned/prohibited persons are restricted.

Bybit explicitly states that:

- users must not misrepresent location/residence;
- if a user is determined to be in an excluded jurisdiction or to have provided false residence/location information, Bybit may terminate the account and liquidate open positions;
- sanctioned/prohibited persons are not eligible.

This policy can change at Bybit's discretion and must be checked again before deployment.

### KYC and location

Live deployment must use truthful KYC/residence information.

Do not design this project around VPN-based jurisdiction evasion, false residence, proxy identity, or bypassing sanctions/KYC controls.

If the verified residence is unsupported at launch time, live deployment is blocked.

## 4. Fiat funding / withdrawal constraints

Current Bybit fiat-service page reviewed on 2026-09-30:

https://www.bybit.com/en/help-center/article/List-of-Restricted-Issuing-Countries-for-Fiat-Service

At that snapshot:

- Russian KYC users are restricted to RUB for Bybit's general fiat service;
- Russia is restricted for several SEPA/SWIFT provider routes.

This is separate from derivatives-trading availability.

Therefore a viable live system needs a documented, lawful path for:

    fiat / crypto funding
    -> Bybit account
    -> trading collateral
    -> withdrawal
    -> final bank/wallet destination

The existence of a trading API does not prove that funding and withdrawal rails will work reliably.

## 5. Pre-live account-access gate

Immediately before real-money deployment, verify and archive:

1. Bybit current Service Restricted Countries page.
2. Current Bybit Platform Terms & Conditions.
3. KYC country/residence shown on the account.
4. Account derivatives eligibility.
5. API-key availability for derivatives.
6. Whether IP whitelisting is supported and enabled.
7. Deposit methods actually available to the verified account.
8. Withdrawal methods actually available to the verified account.
9. Fiat-provider country restrictions.
10. Any account-specific compliance warnings/limits.
11. Current Russian law / Bank of Russia implementing acts.
12. Current tax/reporting requirements.

Save the review date, relevant terms/page hashes, KYC country, available products, available deposit/withdrawal routes, and regulatory-review notes.

No real-money mode may start if this gate is stale.

Recommended maximum age at launch: **7 calendar days**.

If Bybit or Russian regulation changes materially, run the gate again immediately.

## 6. Small-value funding/withdrawal test

Before the bot is permitted to trade meaningful capital:

1. deposit the smallest practical test amount through the intended lawful route;
2. verify final account credit;
3. perform no-risk / minimal-risk account checks;
4. withdraw a small test amount through the intended route;
5. confirm receipt at the final destination;
6. archive transaction IDs and actual fees;
7. only then approve the funding path.

A successful deposit does not prove withdrawals will work.

A successful crypto withdrawal does not prove bank/off-ramp acceptance.

## 7. Account-block / forced-close risk

The system must treat exchange-account restriction as a real operational risk.

Possible causes include:

- change in Bybit jurisdiction policy;
- KYC/residence mismatch;
- sanctions screening;
- AML / source-of-funds review;
- unsupported fiat provider;
- suspicious login/location pattern;
- exchange compliance review.

Engineering implications:

- never keep more capital on the venue than required by the approved operating plan;
- always use exchange-side protective stops;
- continuously reconcile whether trading/API permissions remain active;
- alert immediately on API permission/authorization changes;
- provide a manual close/withdrawal runbook;
- do not assume an account restriction will preserve normal API access.

## 8. Tax/audit ledger requirements

Regardless of jurisdictional reporting status, retain enough data to reconstruct:

- every deposit and withdrawal;
- blockchain transaction ID where applicable;
- every order/fill;
- fee currency and fee amount;
- funding payment;
- realized PnL;
- transfers between Bybit account types/subaccounts;
- conversion trades;
- timestamps in UTC;
- asset quantities and quote values;
- Decision Cost Model vs actual execution differences.

Keep raw exchange transaction logs separately from strategy metrics.

Strategy net_R is not a substitute for tax/accounting records.

## 9. Deployment decision

The project may proceed through data audit, historical backtest, validation, holdout, shadow live, and demo without assuming that real-money Bybit deployment will necessarily be lawful or operationally available at the end.

Real-money deployment requires all of:

- strategy PASS;
- current Bybit eligibility PASS;
- funding/withdrawal test PASS;
- current legal/tax review PASS;
- security/infrastructure PASS.

Failure of the live-compliance gate does not invalidate the strategy research. It blocks deployment until a lawful and operational venue/path is available.
