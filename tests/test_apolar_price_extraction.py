from app.real_estate.extractor import extract_structured_fields
from collectors.real_estate.direct_agencies_collector import _extract_apolar_reference


def test_extract_apolar_reference_from_listing_url():
    url = (
        "https://www.apolar.com.br/alugar/curitiba/sitio-cercado/"
        "alugar-residencial-apartamento-curitiba-sitio-cercado-100127"
    )

    assert _extract_apolar_reference(url) == "100127"


def test_extract_apolar_api_price_from_frontend_payload():
    payload = {
        "url": "https://www.apolar.com.br/alugar/curitiba/sitio-cercado/x-100127",
        "agency_id": "apolar",
        "strategy": "sitemap_api",
        "raw_data": {
            "transacao": "Locacao",
            "referencia": 100127,
            "tipo": "Apartamento",
            "finalidade": "Residencial",
            "bairro": "Sitio Cercado",
            "cidade": "Curitiba",
            "area_total": 55,
            "valor_considerado": 1200,
            "valoraluguel": 1500,
            "valoraluguelliquido": 1200,
            "iptu": 23.79,
        },
    }

    fields = extract_structured_fields(payload)

    assert fields["agency_id"] == "apolar"
    assert fields["listing_code"] == "100127"
    assert fields["listing_type"] == "aluguel"
    assert fields["price"] == 1200.0
    assert fields["price_raw"] == "1200"
    assert fields["extraction_confidence"] == "high"


def test_extract_imobiliariamaringa_static_html_price():
    payload = {
        "url": "https://imobiliariamaringa.com.br/imovel/excelente-sobrado/",
        "agency_id": "imobiliariamaringa",
        "strategy": "linked_raw_html",
        "raw_html_snippet": (
            "<html><head><title>Excelente sobrado conceito jardim moncoes zona sul</title></head>"
            "<body><h1>Excelente sobrado conceito jardim moncoes zona sul</h1>"
            "<p>Jardim Moncoes, Maringá - PR</p><p>Comprar</p><p>R$2.000.000,00</p>"
            "<p>344 m²</p></body></html>"
        ),
    }

    fields = extract_structured_fields(payload)

    assert fields["agency_id"] == "imobiliariamaringa"
    assert fields["city"] == "Maringá"
    assert fields["listing_type"] == "venda"
    assert fields["price"] == 2_000_000.0
    assert fields["extraction_confidence"] == "high"
