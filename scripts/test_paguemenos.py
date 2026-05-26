import httpx, asyncio, re, sys
sys.path.insert(0, "/app")

async def test():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer": "https://www.paguemenos.com.br/",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as c:
        r = await c.get("https://www.paguemenos.com.br/fralda-descartavel-infantil-pampers-confort-sec-xxxg-mais-de-19kg-pacote-44-unidades-leve-mais-pague-menos/p")
        print("Status:", r.status_code, "Size:", len(r.text))

        # Search for JSON-LD tags
        ld_count = r.text.count("application/ld+json")
        print("ld+json count:", ld_count)

        # Find all ld+json
        found_product = False
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.text, re.DOTALL | re.IGNORECASE):
            content = m.group(1).strip()
            if "Product" in content or "price" in content.lower():
                print("JSON-LD Product found:", content[:600])
                found_product = True
                break
        if not found_product:
            print("No JSON-LD Product found")

        # VTEX __STATE__
        state_match = re.search(r"__STATE__\s*=\s*\{", r.text)
        print("__STATE__ found:", bool(state_match))

        # Price in HTML
        prices = re.findall(r'"(?:sellingPrice|Price|price)"\s*:\s*([\d.]+)', r.text[:200000])
        print("Prices found:", prices[:5])

        # Anti-bot check more carefully
        has_cf_challenge = any(x in r.text.lower() for x in ["just a moment", "checking your browser", "cf-challenge", "ddos protection by cloudflare", "attention required! | cloudflare"])
        has_captcha_challenge = any(x in r.text.lower() for x in ["challenge-form", "prove you are not a robot", "verificação de segurança"])
        print("CF challenge:", has_cf_challenge)
        print("CAPTCHA challenge:", has_captcha_challenge)

        # Check what triggers the false positive
        has_cf_cdn = "cdnjs.cloudflare.com" in r.text
        has_recaptcha = "recaptcha" in r.text.lower()
        print("CF CDN (false pos):", has_cf_cdn)
        print("reCAPTCHA (false pos):", has_recaptcha)

asyncio.run(test())
