"""
Gera o feed de produtos da LiveStore Online para o ChatGPT Ads (OpenAI)
a partir da API da Nuvemshop.

Saida: site/feed.csv (publicado no GitHub Pages) e site/relatorio.txt.

Variaveis de ambiente:
  NUVEMSHOP_TOKEN  token do aplicativo sob medida (secret do GitHub)
  STORE_ID         ID da loja na Nuvemshop
"""

import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests

API = "https://api.nuvemshop.com.br/v1/{store}/{path}"
USER_AGENT = "LagosFeedChatGPTAds (Lagos Assessoria)"

SELLER_NAME = "LiveStore Online"
SELLER_URL = "https://www.livestoreonline.com.br"
RETURN_POLICY = "https://www.livestoreonline.com.br/politica-de-devolucao/"
COUNTRY = "BR"
CURRENCY = "BRL"
UTM = {
    "utm_source": "chatgpt",
    "utm_medium": "cpc",
    "utm_campaign": "chatgpt_ads_catalogo",
}

COLUMNS = [
    "item_id", "title", "description", "url", "brand", "image_url",
    "additional_image_urls", "price", "sale_price", "availability",
    "condition", "gtin", "product_category", "group_id",
    "listing_has_variations", "variant_dict", "seller_name", "seller_url",
    "return_policy", "target_countries", "store_country",
    "is_eligible_search", "is_eligible_checkout", "is_ads_eligible",
]


def pt(value):
    """Campos multilingues da Nuvemshop vem como {"pt": "..."}."""
    if isinstance(value, dict):
        return value.get("pt") or next((v for v in value.values() if v), "")
    return value or ""


def get_all(session, store, path, params=None):
    """Busca todas as paginas de um endpoint da Nuvemshop."""
    items, page = [], 1
    while True:
        query = {"per_page": 200, "page": page, **(params or {})}
        url = API.format(store=store, path=path)
        for attempt in range(5):
            resp = session.get(url, params=query, timeout=60)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            break
        # A Nuvemshop responde 404 quando a pagina passa da ultima
        if resp.status_code == 404 and page > 1:
            break
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < query["per_page"]:
            break
        page += 1
    return items


def clean_text(raw, limit):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def money(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None, 0.0
    if amount <= 0:
        return None, 0.0
    return f"{amount:.2f} {CURRENCY}", amount


def valid_gtin(code):
    digits = re.sub(r"\D", "", code or "")
    if len(digits) not in (8, 12, 13, 14) or digits != (code or "").strip():
        return ""
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1)
                for i, d in enumerate(reversed(body)))
    return digits if (10 - total % 10) % 10 == check else ""


def add_params(url, extra):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(extra)
    return urlunsplit(parts._replace(query=urlencode(query)))


def category_path(cat, by_id):
    names, seen = [], set()
    while cat and cat["id"] not in seen:
        seen.add(cat["id"])
        names.append(pt(cat.get("name")))
        cat = by_id.get(cat.get("parent"))
    return " > ".join(reversed([n for n in names if n]))


def build_rows(products, categories):
    by_id = {c["id"]: c for c in categories}
    rows, skipped = [], Counter()
    skipped_names = []

    for product in products:
        title_base = clean_text(pt(product.get("name")), 150)
        if not product.get("published", True):
            skipped["nao publicado"] += 1
            continue
        brand = (product.get("brand") or "").strip()
        if not brand:
            skipped["sem marca"] += 1
            skipped_names.append(f"sem marca: {title_base}")
            continue
        url_base = product.get("canonical_url") or ""
        if not url_base.startswith("https://"):
            skipped["sem URL https"] += 1
            continue

        images = sorted(product.get("images") or [],
                        key=lambda i: i.get("position") or 0)
        image_by_id = {i["id"]: i["src"] for i in images if i.get("src")}
        all_images = [i["src"] for i in images if i.get("src", "").startswith("https://")]
        if not all_images:
            skipped["sem foto"] += 1
            skipped_names.append(f"sem foto: {title_base}")
            continue

        description = clean_text(pt(product.get("description")), 5000) or title_base
        cats = product.get("categories") or []
        paths = [category_path(by_id.get(c["id"], c), by_id) for c in cats]
        # Usa a categoria mais especifica (caminho mais longo), nao a primeira da lista
        product_category = max(paths, key=lambda p: (p.count(" > "), len(p)), default="")

        attributes = [pt(a) for a in (product.get("attributes") or [])]
        variants = product.get("variants") or []
        has_variations = len(variants) > 1

        for variant in variants:
            price, price_value = money(variant.get("price"))
            if not price:
                skipped["sem preco"] += 1
                continue
            sale, sale_value = money(variant.get("promotional_price"))
            if not sale or sale_value >= price_value:
                sale = ""

            manages_stock = variant.get("stock_management", True)
            stock = variant.get("stock")
            in_stock = (not manages_stock) or stock is None or (stock or 0) > 0
            if not in_stock:
                skipped["sem estoque"] += 1
                continue

            values = [pt(v) for v in (variant.get("values") or [])]
            variant_dict = dict(zip(attributes, values)) if attributes and values else {}
            title = title_base
            if has_variations and values:
                title = clean_text(f"{title_base} {' '.join(values)}", 150)

            main_image = image_by_id.get(variant.get("image_id")) or all_images[0]
            extra_images = [u for u in all_images if u != main_image][:10]

            url = url_base
            if has_variations:
                url = add_params(url, {"variant": str(variant["id"])})
            url = add_params(url, UTM)

            rows.append({
                "item_id": str(variant["id"]),
                "title": title,
                "description": description,
                "url": url,
                "brand": brand,
                "image_url": main_image,
                "additional_image_urls": ",".join(u.replace(",", "%2C") for u in extra_images),
                "price": price,
                "sale_price": sale,
                "availability": "in_stock",
                "condition": "new",
                "gtin": valid_gtin(variant.get("barcode")),
                "product_category": product_category,
                "group_id": f"P{product['id']}" if has_variations else "",
                "listing_has_variations": "true" if has_variations else "false",
                "variant_dict": json.dumps(variant_dict, ensure_ascii=False) if has_variations and variant_dict else "",
                "seller_name": SELLER_NAME,
                "seller_url": SELLER_URL,
                "return_policy": RETURN_POLICY,
                "target_countries": json.dumps([COUNTRY]),
                "store_country": COUNTRY,
                "is_eligible_search": "true",
                "is_eligible_checkout": "false",
                "is_ads_eligible": "true",
            })
    return rows, skipped, skipped_names


def main():
    token = os.environ.get("NUVEMSHOP_TOKEN")
    store = os.environ.get("STORE_ID")
    if not token or not store:
        sys.exit("Faltam as variaveis NUVEMSHOP_TOKEN e STORE_ID.")

    session = requests.Session()
    session.headers.update({
        "Authentication": f"bearer {token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    })

    products = get_all(session, store, "products", {"published": "true"})
    try:
        categories = get_all(session, store, "categories")
    except requests.HTTPError:
        categories = []  # token sem leitura de categorias: segue sem o caminho completo

    rows, skipped, skipped_names = build_rows(products, categories)
    if not rows:
        sys.exit("Nenhum produto valido. Feed anterior mantido.")

    os.makedirs("site", exist_ok=True)
    with open("site/feed.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    report = [
        f"Produtos lidos na Nuvemshop: {len(products)}",
        f"Linhas no feed: {len(rows)}",
        "Fora do feed:",
        *[f"  {motivo}: {qtd}" for motivo, qtd in skipped.most_common()],
        "",
        "Produtos para corrigir no cadastro:",
        *[f"  {n}" for n in skipped_names],
    ]
    with open("site/relatorio.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    open("site/.nojekyll", "w").close()
    print("\n".join(report[:4 + len(skipped)]))


if __name__ == "__main__":
    main()
