#!/usr/bin/env python3
"""Find Shopify stores worth pitching an exchange app to.

A good prospect sells variant-heavy apparel/footwear: those stores have
constant size-swap exchanges, which is the whole problem we solve.

Every Shopify store exposes /products.json publicly. We read page one,
look for a size-like option with enough values, and score from there.

usage:
    ./qualify_stores.py domains.txt              # one domain per line
    cat domains.txt | ./qualify_stores.py -      # or stdin
    ./qualify_stores.py domains.txt --min-size 4 --all
    ./qualify_stores.py --self-check             # no network
"""

import argparse
import concurrent.futures
import csv
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 12
UA = "Mozilla/5.0 (compatible; store-qualifier/1.0)"

# Shopify lets merchants rename options, so match loosely.
SIZE_WORDS = {"size", "sizes", "shoe size", "uk size", "us size", "eu size",
              "waist", "length", "fit", "band size", "cup size"}

APPAREL_WORDS = {"shirt", "tee", "top", "dress", "pant", "trouser", "jean",
                 "short", "skirt", "jacket", "coat", "hoodie", "sweater",
                 "jumper", "legging", "bra", "sock", "shoe", "boot", "sneaker",
                 "sandal", "denim", "blazer", "knit", "swim", "bikini"}


def normalize(domain):
    """Strip scheme, path and www so 'https://www.x.com/collections' -> 'x.com'."""
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0].split("?")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def fetch_products(domain):
    """Return (products, error). Non-Shopify stores fail here harmlessly."""
    url = f"https://{domain}/products.json?limit=250"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            final_url = r.geturl()
            body = r.read(4_000_000)
        # Pre-launch stores bounce products.json to the lock screen. They're
        # Shopify, just not open yet - label them apart from non-Shopify.
        if "/password" in final_url:
            return None, "shopify, password-protected"
        data = json.loads(body)
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except urllib.error.URLError as e:
        return None, f"unreachable ({e.reason})"
    except json.JSONDecodeError:
        return None, "not shopify (no product json)"
    except Exception as e:  # socket timeouts, bad TLS, redirect loops
        return None, f"{type(e).__name__}"
    if not isinstance(data, dict) or "products" not in data:
        return None, "not shopify (unexpected json)"
    return data["products"], None


def product_options(product):
    """Yield (option_name, values) pairs, tolerating both known shapes."""
    opts = product.get("options") or []
    for o in opts:
        if isinstance(o, dict):
            yield str(o.get("name", "")).strip(), list(o.get("values") or [])
        elif isinstance(o, str):
            # Some responses give bare option names with values on variants.
            yield o.strip(), []


def qualify(products, min_size_values=3):
    """Score a product list. Pure function - the self-check exercises this.

    Returns a dict describing why the store did or didn't qualify.
    """
    result = {
        "products_seen": len(products),
        "size_products": 0,
        "max_size_values": 0,
        "apparel_hits": 0,
        "size_option_names": set(),
        "qualified": False,
    }
    if not products:
        result["reason"] = "no products"
        return result

    for p in products:
        title = str(p.get("title", "")).lower()
        ptype = str(p.get("product_type", "")).lower()
        if any(w in title or w in ptype for w in APPAREL_WORDS):
            result["apparel_hits"] += 1

        for name, values in product_options(p):
            if name.lower() not in SIZE_WORDS:
                continue
            result["size_option_names"].add(name)
            n = len(values)
            result["max_size_values"] = max(result["max_size_values"], n)
            if n >= min_size_values:
                result["size_products"] += 1

    result["size_option_names"] = sorted(result["size_option_names"])
    # Require a real catalogue of size-varied product, not one stray hoodie.
    result["qualified"] = result["size_products"] >= 3
    if result["qualified"]:
        result["reason"] = "ok"
    elif result["max_size_values"] == 0:
        result["reason"] = "no size option"
    else:
        result["reason"] = f"only {result['size_products']} size-varied products"
    return result


def check(domain, min_size_values):
    d = normalize(domain)
    if not d:
        return None
    products, err = fetch_products(d)
    if err:
        return {"domain": d, "shopify": False, "qualified": False,
                "reason": err, "products_seen": 0, "size_products": 0,
                "max_size_values": 0, "apparel_hits": 0, "size_option_names": []}
    r = qualify(products, min_size_values)
    r.update({"domain": d, "shopify": True})
    return r


def self_check():
    """Offline assertions on the scoring logic."""
    assert normalize("https://www.Foo.com/collections/all") == "foo.com"
    assert normalize("foo.com") == "foo.com"

    tee = {"title": "Cotton Tee", "product_type": "Shirts",
           "options": [{"name": "Size", "values": ["S", "M", "L"]}]}
    mug = {"title": "Enamel Mug", "product_type": "Drinkware",
           "options": [{"name": "Title", "values": ["Default Title"]}]}
    two = {"title": "Cap", "product_type": "Hats",
           "options": [{"name": "Size", "values": ["S", "M"]}]}

    r = qualify([tee, tee, tee, mug])
    assert r["qualified"], r
    assert r["size_products"] == 3, r
    assert r["apparel_hits"] == 3, r

    r = qualify([mug, mug])
    assert not r["qualified"] and r["reason"] == "no size option", r

    # Two size values is below the default threshold, so it must not count.
    r = qualify([two, two, two])
    assert not r["qualified"], r
    assert r["max_size_values"] == 2, r

    # Lowering the bar lets the same catalogue through.
    r = qualify([two, two, two], min_size_values=2)
    assert r["qualified"], r

    r = qualify([])
    assert not r["qualified"] and r["reason"] == "no products", r

    # String-shaped options must not crash the parser.
    assert qualify([{"title": "x", "options": ["Size"]}])["max_size_values"] == 0

    print("self-check ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("domains", nargs="?", help="file of domains, or - for stdin")
    ap.add_argument("--min-size", type=int, default=3,
                    help="size values needed to count a product (default 3)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--all", action="store_true", help="also print non-qualifying stores")
    ap.add_argument("--self-check", action="store_true", help="run offline tests and exit")
    args = ap.parse_args()

    if args.self_check:
        self_check()
        return

    if not args.domains:
        ap.error("need a domains file (or - for stdin), or --self-check")

    src = sys.stdin if args.domains == "-" else open(args.domains)
    domains = [ln for ln in (l.strip() for l in src) if ln and not ln.startswith("#")]
    if src is not sys.stdin:
        src.close()

    w = csv.writer(sys.stdout)
    w.writerow(["domain", "qualified", "reason", "size_products",
                "max_size_values", "apparel_hits", "products_seen", "size_options"])

    n_ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, d, args.min_size): d for d in domains}
        for f in concurrent.futures.as_completed(futures):
            r = f.result()
            if not r:
                continue
            if r["qualified"]:
                n_ok += 1
            elif not args.all:
                continue
            w.writerow([r["domain"], r["qualified"], r["reason"], r["size_products"],
                        r["max_size_values"], r["apparel_hits"], r["products_seen"],
                        "|".join(r["size_option_names"])])
            sys.stdout.flush()

    print(f"# {n_ok}/{len(domains)} qualified", file=sys.stderr)


if __name__ == "__main__":
    main()
