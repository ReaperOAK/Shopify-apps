# Shopify Apps & Merchant Exchange Engine

> Automated merchant exchange/returns engine design and high-volume Shopify store qualification pipeline.

## 🛍️ Overview

1. **Exchange Engine Architecture (`docs/superpowers/specs/`)**:
   - Replaces friction-heavy eCommerce refund workflows with an instant 1-click exchange & store credit routing system.
   - Built to integrate directly via Shopify Admin GraphQL API and Webhooks (`orders/fulfilled`, `refunds/create`).
   - Mitigates inventory reservation race conditions during flash exchanges.

2. **Store Qualification Engine (`qualify_stores.py`)**:
   - Automated store crawler and revenue/volume heuristic scorer.
   - Parses storefront HTML, schema.org microdata, platform signals (e.g. Klaviyo, Loop Returns, Gorgias script tags), and exchange policy compliance.
   - Outputs qualified merchant lead matrices (`exchange_leads.csv`).
