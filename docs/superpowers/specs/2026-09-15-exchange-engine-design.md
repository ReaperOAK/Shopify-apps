# Exchange Engine — Design

**Date:** 2026-09-15
**Status:** Approved, pending implementation plan

A Shopify app that makes exchanges self-service for the buyer and zero-touch
for the merchant, built entirely on Shopify's native Return/Exchange
primitives.

---

## 1. Why this exists

Shopify has native exchanges in the Admin GraphQL API. They work well: the
exchange stays on the original order, inventory restocks and reallocates
automatically, and the money is computed correctly including tax. All of that
was verified live against a development store on 2026-09-15 (see §9).

Two gaps remain, both verified:

**Gap 1 — the hold is never released.** When a buyer trades up, Shopify holds
the replacement's fulfillment order with reason `AWAITING_PAYMENT` and invoices
the balance. When the buyer pays, the order goes `PAID` with
`totalOutstandingSet == 0` — **and the hold stays**. Re-checked at +20s, +40s
and +60s: unchanged. Without intervention the customer has paid and will never
receive the item, silently. Nobody is alerted.

**Gap 2 — the buyer cannot request an exchange.** The Customer Account API's
`orderRequestReturn` accepts return line items only. There is no exchange
mutation on the buyer-facing API. Shopify's own self-serve returns flow
structurally cannot offer a replacement variant.

Gap 1 is the hook. Gap 2 is the product.

### Risk to hold in view

Gap 1 is a *defect*, not a moat. Shopify could close it in a sprint, and the
headline demo would evaporate. The durable product is the buyer portal, the
policy engine, and the merchant never thinking about exchanges — with the
hold-release as one feature among them, not the foundation.

---

## 2. Scope

**v1 ships:** buyer self-service portal, policy engine, the reconciler
(hold-release + guard), out-of-stock handling with preorder and store credit,
merchant settings and an event log.

**v1 excludes:** analytics dashboards, return shipping labels, international
returns and duties, multi-item exchanges, warranty flows, bonus-credit
incentives.

**Target merchant:** stores using Shopify's native returns who currently do
exchanges by hand in the admin. No incumbent to displace, so onboarding is
minutes. Merchants switching off Loop/AfterShip are a later phase.

**Pricing:** $29/month. A free tier capped by exchange volume may be added
later purely as a review-farming channel.

---

## 3. Architecture

Shopify embedded app on the official React Router template (Node). Postgres
for app state.

**Offline access token is required**, not optional. All the valuable work is
background webhook work, and `orderCreate` refuses online tokens outright
(verified — the CLI's `store auth` online token is rejected).

Five units, each independently testable:

| Unit | Responsibility | Depends on |
|---|---|---|
| **State machine** | Owns the exchange lifecycle. Every transition idempotent and logged. | Postgres, Shopify Admin API |
| **Reconciler** | Releases holds, enforces the unpaid-ship guard, expires stale waits. | State machine, webhooks |
| **Buyer portal** | Customer Account API auth, variant picker, delta display, confirm. | Policy engine, state machine |
| **Policy engine** | Window, eligibility, approval mode, instant-exchange toggle. Pure functions. | — |
| **Merchant admin** | Settings, exchange list, event log. | State machine |

The policy engine is pure functions with no I/O, so eligibility rules are
unit-testable without a store.

---

## 4. When to process — the decision that matters

Our first test called `returnProcess` immediately, which restocked the
returned item **while the customer still physically had it**. That is the
phantom-inventory bug, and it would oversell.

So the lifecycle splits those two moments:

- `returnCreate` at **request time** — records intent, moves no stock.
- `returnProcess` at **receipt time** — restocks the returned item, allocates
  the replacement, opens the balance, creates the held fulfillment order.

**Default: process-on-receipt.** The merchant marks the return received (or a
carrier scan does, later).

**Setting: instant exchange**, default off. Processes immediately so the
replacement ships before the return lands. This is a real retention feature
that Loop charges for, but shipping goods against an unreturned item is the
merchant's risk to accept explicitly, never our default.

---

## 5. State machine

```
REQUESTED ─→ PENDING_APPROVAL ─→ DECLINED
                   │
                   ↓ approved (or auto-approve on)
             RETURN_CREATED
                   │
                   ↓ item received (or instant-exchange on)
               PROCESSED ──→ restock + allocate + invoice
                   │
        ┌──────────┴──────────┐
        ↓ balance > 0          ↓ balance ≤ 0
  AWAITING_PAYMENT        READY_TO_SHIP
        │      │                │
   paid │      │ expired        │
        ↓      ↓                ↓
  READY_TO_SHIP  EXPIRED   hold released
        │                       │
        └──────→ SHIPPED ───────┘ ─→ COMPLETED
```

Failure states branch rather than dead-end, each retryable and each holding the
last error: `INVENTORY_UNAVAILABLE`, `PAYMENT_FAILED`, `SHOPIFY_ERROR`.

Every transition writes an immutable event row: timestamp, from-state,
to-state, actor (buyer / merchant / system), and the Shopify mutation plus
response. The merchant-visible timeline reads from this. It is cheap to build
and disproportionately valuable for trust in an app that moves money.

---

## 6. The reconciler

Two rules, both verified against live Shopify:

1. **Release** — hold reason is `AWAITING_PAYMENT` **and**
   `totalOutstandingSet == 0` → call `fulfillmentOrderReleaseHold`.
2. **Guard** — never release while outstanding > 0. Verified that Shopify
   *will* let you release an unpaid hold and ship free goods. The guard is
   ours to enforce; Shopify enforces nothing here.

Triggered by webhooks — `orders/paid`, `returns/processed`,
`fulfillment_orders/order_routing_complete` — with a **periodic sweep behind
them**. Webhooks get dropped, and a missed release means a customer paid and
received nothing. The sweep is the safety net, not an optimisation.

---

## 7. Out of stock

The buyer sees every variant. In stock proceeds normally. Out of stock offers
two paths:

- **Preorder** — ships on restock, bounded by a merchant-set expiry (default
  30 days). On expiry the buyer is offered store credit or a refund rather
  than waiting indefinitely.
- **Store credit** — issued via `returnProcess` with
  `refundMethodAllocation: STORE_CREDIT`, confirmed present in the schema.

The expiry is non-negotiable. Without it, "where is my exchange" becomes the
top support ticket and the app's worst reviews.

---

## 8. Idempotency

Every Shopify mutation carries a deterministic key derived from
`(exchange_id, transition)`. Shopify already forces this on inventory
mutations — `@idempotent(key:)` is mandatory and must sit on the **field**,
not the operation.

A replayed webhook must never produce a second return, a second invoice, or a
second hold release. Webhook handlers dedupe on Shopify's delivery id before
touching the state machine.

---

## 9. Verified API behaviour

Confirmed live on `exchange-engine-test.myshopify.com`, API version 2026-07,
across two complete exchange runs.

**Working sequence:**

```
returnCalculate (query)      → delta shown pre-commit, tax-correct
returnCreate                 → same order, no second order created
suggestedFinancialOutcome    → InvoiceReturnOutcome, exact delta
returnProcess                → restock + allocate + hold + balance
orderInvoiceSend             → buyer gets a real payable checkout
fulfillmentOrderReleaseHold  → ON_HOLD → OPEN   (never automatic)
fulfillmentCreate            → shipped
```

**Scopes:** `read_returns`, `write_returns`, `read_orders`, `write_orders`,
plus product/inventory/fulfillment scopes. There is **no `better_exchanges`
scope** in current docs.

**Documentation drift found — the published guides are wrong in six places:**

1. `returnCalculate` is a **query**, not a mutation.
2. The `returnReason` enum is gone; use `returnReasonDefinitionId` from
   `returnReasonDefinitions`.
3. `CalculatedReturn` has no `financialSummary` in 2026-07.
4. `@idempotent(key:)` is required on inventory mutations, at field level.
5. `orderCreate` requires an **offline** token.
6. `ExchangeLineItem` fields differ between 2026-07 and 2026-10
   (`variantTitle`, `title`, `productId` exist only in the newer version).

**`returnProcess` has no invoice option.** Its `financialTransfer` input
accepts only `issueRefund`. Shopify tells you via `suggestedFinancialOutcome`
that the outcome should be an invoice, then gives you no way to instruct it.
Process with no financial transfer, then send the invoice separately.

**Order totals:** `totalPriceSet` is gross and sums both line items ($100 in
our test). `currentTotalPriceSet` is the correct net ($60). Every buyer-facing
surface uses the net figure. Only third-party analytics reading `total_price`
would be misled.

---

## 10. Testing

`exchange-engine-test.myshopify.com` is the integration fixture. It already
holds the product with size variants, two completed exchanges, and Bogus
Gateway configured for real payment tests (card `1` approves).

- **Policy engine** — pure unit tests, no store required.
- **Reconciler** — tested against recorded webhook payloads, including
  replays and out-of-order delivery.
- **State machine** — property test that every transition is idempotent under
  repetition.
- **End-to-end** — the full sequence in §9 against the dev store.

---

## 11. Open items

- Portal surface: standalone hosted page versus Customer Account UI
  extension. Deferred to implementation planning, once the variant-picker's
  needs are concrete.
- Current demand is **unmeasured**, not proven. The review quotes behind the
  original pitch date to 2017, 2018 and 2020. A targeted sweep of the returns
  category is running to establish whether the complaint is still live.
- Buyer-facing invoice email delivery is untested; API path is confirmed.
