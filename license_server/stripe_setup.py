"""
stripe_setup.py — One-time Stripe product + price setup.

Run once after setting STRIPE_SECRET_KEY:
    STRIPE_SECRET_KEY=sk_live_... python stripe_setup.py

Creates:
  - 3 subscription products (Starter / Pro / Elite)
  - Monthly prices for each
  - Webhook endpoint pointing to your license server
  - Prints webhook signing secret to add to your .env
"""

import os
import json
import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
if not stripe.api_key:
    print("ERROR: Set STRIPE_SECRET_KEY environment variable first.")
    exit(1)

LICENSE_SERVER_URL = os.environ.get(
    "LICENSE_SERVER_URL",
    "https://your-license-server.railway.app"
)

PLANS = [
    {
        "name":        "OMNI-ICT Starter",
        "description": "1 MT5 account · MODERATE risk · Paper mode included · Telegram control",
        "plan":        "starter",
        "price_cents": 4900,   # $49/month
    },
    {
        "name":        "OMNI-ICT Pro",
        "description": "3 MT5 accounts · All risk modes · Priority support · Custom rules",
        "plan":        "pro",
        "price_cents": 9900,   # $99/month
    },
    {
        "name":        "OMNI-ICT Elite",
        "description": "Unlimited accounts · White-glove setup · 1-on-1 onboarding call",
        "plan":        "elite",
        "price_cents": 19900,  # $199/month
    },
]

created = []

print("\n── Creating Stripe products and prices ──────────────\n")

for plan in PLANS:
    # Create product
    product = stripe.Product.create(
        name        = plan["name"],
        description = plan["description"],
        metadata    = {"plan": plan["plan"]},
    )
    print(f"✓ Product: {plan['name']}  id={product.id}")

    # Create monthly price
    price = stripe.Price.create(
        product    = product.id,
        unit_amount= plan["price_cents"],
        currency   = "usd",
        recurring  = {"interval": "month"},
        metadata   = {"plan": plan["plan"]},
    )
    print(f"  Price:   ${plan['price_cents']/100:.2f}/mo  id={price.id}")

    created.append({
        "plan":       plan["plan"],
        "product_id": product.id,
        "price_id":   price.id,
        "price_usd":  plan["price_cents"] / 100,
    })

# Create webhook endpoint
print(f"\n── Creating webhook endpoint → {LICENSE_SERVER_URL} ──\n")
webhook = stripe.WebhookEndpoint.create(
    url            = f"{LICENSE_SERVER_URL}/stripe/webhook",
    enabled_events = [
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "invoice.payment_failed",
    ],
    description = "OMNI-ICT license server",
)
print(f"✓ Webhook created  id={webhook.id}")
print(f"\n  ┌─────────────────────────────────────────────────────────┐")
print(f"  │  Add this to your license server .env:                  │")
print(f"  │  STRIPE_WEBHOOK_SECRET={webhook.secret}  │")
print(f"  └─────────────────────────────────────────────────────────┘")

# Save summary
summary = {
    "webhook_secret": webhook.secret,
    "plans":          created,
}
with open("stripe_products.json", "w") as f:
    json.dump(summary, f, indent=2)

print("\n── Summary saved to stripe_products.json ─────────────\n")
print("Next steps:")
print("  1. Add STRIPE_WEBHOOK_SECRET to your license server .env")
print("  2. Create payment links for each price in the Stripe dashboard")
print("     (Products → select each → Create payment link)")
print("  3. Use those payment links on your website/landing page")
print()
for p in created:
    print(f"  {p['plan'].upper():8s}  ${p['price_usd']:.2f}/mo  price_id={p['price_id']}")
print()
