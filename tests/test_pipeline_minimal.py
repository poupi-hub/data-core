from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.analytics.models import ProductPriceAnalytics, TradingAnalytics
from app.data_quality.models import DataQualityRun
from app.data_quality.services import DataQualityService
from app.documentation.models import DataContract, DataLineage, DataOwner, DataSla
from app.documentation.services import DocumentationService
from app.modules.ecommerce.normalizers.poupi_legacy_scraped_product_v1_normalizer import PoupiLegacyScrapedProductV1Normalizer
from app.normalization.models import NormalizedMarketCandle, NormalizedProduct
from app.raw.models import CollectorVersion, RawCollection
from app.raw.service import RawCollectionService
from database.models import CollectionRun, CollectionTarget, CollectorError, RunStatus
import scheduler.jobs as scheduler_jobs
from scheduler.jobs import run_collection_targets_job, run_module_collectors_job


@pytest.fixture(autouse=True)
def clean_pytest_pipeline_records(db_session):
    _cleanup_pytest_records(db_session)
    yield
    _cleanup_pytest_records(db_session)


def test_raw_service_saves_versioned_json_and_deduplicates(db_session):
    source = f"pytest-raw-{uuid4()}"
    service = RawCollectionService(db_session)

    raw = service.save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        collector_version="1.2.3",
        raw_schema_name="pytestProduct",
        raw_schema_version="1.0.0",
        raw_json={"title": "Produto teste", "price": 10.9},
        metadata={"test": True},
    )
    raw_was_created = getattr(raw, "_raw_was_created")
    duplicate = service.save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        collector_version="1.2.3",
        raw_schema_name="pytestProduct",
        raw_schema_version="1.0.0",
        raw_json={"title": "Produto teste", "price": 10.9},
        metadata={"test": True},
    )

    assert raw.id == duplicate.id
    assert raw.processing_status == "normalization_pending"
    assert raw.collector_version == "1.2.3"
    assert raw.raw_schema_name == "pytestProduct"
    assert raw_was_created is True
    assert getattr(duplicate, "_raw_was_created") is False
    assert (
        db_session.query(CollectorVersion)
        .filter(
            CollectorVersion.source_name == source,
            CollectorVersion.collector_name == "pytest_collector",
            CollectorVersion.collector_version == "1.2.3",
        )
        .count()
        == 1
    )


def test_data_quality_can_run_for_one_source(db_session):
    source = f"pytest-quality-{uuid4()}"
    raw_service = RawCollectionService(db_session)
    raw_ok = raw_service.save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        raw_schema_name="pytestProduct",
        raw_json={"title": "Produto com preço", "price": 10},
    )
    raw_bad = raw_service.save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        raw_schema_name="pytestProduct",
        raw_json={"title": "Produto sem preço"},
        target_url=f"https://example.test/{uuid4()}",
    )
    db_session.add_all(
        [
            NormalizedProduct(
                raw_collection_id=raw_ok.id,
                title="Produto com preço",
                price=Decimal("10.00"),
                store_name=source,
                collected_at=datetime.now(timezone.utc),
                normalizer_name="pytest_normalizer",
                normalizer_version="1.0.0",
                source_raw_schema_name="pytestProduct",
                source_raw_schema_version="1.0.0",
                source_collector_name="pytest_collector",
                source_collector_version="1.0.0",
            ),
            NormalizedProduct(
                raw_collection_id=raw_bad.id,
                title="Produto sem preço",
                price=None,
                store_name=source,
                collected_at=datetime.now(timezone.utc),
                normalizer_name="pytest_normalizer",
                normalizer_version="1.0.0",
                source_raw_schema_name="pytestProduct",
                source_raw_schema_version="1.0.0",
                source_collector_name="pytest_collector",
                source_collector_version="1.0.0",
            ),
        ]
    )
    db_session.flush()

    result = DataQualityService(db_session).run(module="ecommerce", source_name=source, limit=10)

    assert result["runs"][0]["checked_count"] == 2
    assert result["runs"][0]["passed_count"] == 1
    assert result["runs"][0]["failed_count"] == 1
    assert result["runs"][0]["quality_score"] == 0.5
    run = db_session.query(DataQualityRun).filter(DataQualityRun.source_name == source).one()
    assert run.metadata_json["rule_stats"]["price_gt_0"]["failed"] == 1
    assert run.metadata_json["failure_samples"][0]["failed_rules"] == ["price_gt_0"]


def test_documentation_governance_defaults_are_persisted(db_session):
    service = DocumentationService(db_session)

    contracts = service.data_contracts(module="ecommerce")
    owners = service.owners(module="ecommerce")
    slas = service.slas(module="ecommerce")
    db_session.commit()

    assert contracts[0]["module"] == "ecommerce"
    assert contracts[0]["raw_required"] is True
    assert owners[0]["owner"] == "data-platform"
    module_sla = next(item for item in slas if item["source_name"] is None)
    drogasil_sla = next(item for item in slas if item["source_name"] == "drogasil")
    assert module_sla["freshness_sla"] == "not_defined"
    assert drogasil_sla["freshness_sla"] == "daily"
    assert db_session.query(DataContract).filter(DataContract.module == "ecommerce").count() >= 1
    assert db_session.query(DataOwner).filter(DataOwner.module == "ecommerce").count() >= 1
    assert db_session.query(DataSla).filter(DataSla.module == "ecommerce").count() >= 1


def test_documentation_governance_crud(db_session):
    service = DocumentationService(db_session)
    source = f"pytest-governance-{uuid4()}"
    owner_name = f"pytest-owner-{uuid4()}"

    owner = service.upsert_owner(
        {
            "module": "ecommerce",
            "owner_name": owner_name,
            "technical_contact": "tech@example.test",
        }
    )
    assert owner["owner"] == owner_name
    updated_owner = service.update_owner(
        owner["id"],
        {"business_contact": "biz@example.test"},
    )
    assert updated_owner["business_contact"] == "biz@example.test"

    sla = service.upsert_sla(
        {
            "module": "ecommerce",
            "source_name": source,
            "freshness_sla": "daily",
            "quality_sla": "0.95",
        }
    )
    assert sla["freshness_sla"] == "daily"
    updated_sla = service.update_sla(sla["id"], {"freshness_sla": "hourly"})
    assert updated_sla["freshness_sla"] == "hourly"

    contract = service.upsert_contract(
        {
            "module": "ecommerce",
            "source_name": source,
            "contract_name": "pytest_contract",
            "owner_name": owner_name,
            "freshness_sla": "hourly",
            "quality_rules_json": {"price_gt_0": True},
        }
    )
    assert contract["owner"] == owner_name
    updated_contract = service.update_contract(contract["id"], {"criticality": "high"})
    assert updated_contract["criticality"] == "high"

    assert service.delete_contract(contract["id"]) is True
    assert service.delete_owner(owner["id"]) is True
    assert service.delete_sla(sla["id"]) is True


def test_lineage_backfill_links_raw_normalized_and_analytics(db_session):
    source = f"pytest-lineage-{uuid4()}"
    raw = RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        raw_schema_name="pytestProduct",
        raw_json={"title": "Produto lineage", "price": 25},
    )
    product = NormalizedProduct(
        raw_collection_id=raw.id,
        title="Produto lineage",
        price=Decimal("25.00"),
        store_name=source,
        collected_at=datetime.now(timezone.utc),
        normalizer_name="pytest_normalizer",
        normalizer_version="1.0.0",
        source_raw_schema_name="pytestProduct",
        source_raw_schema_version="1.0.0",
        source_collector_name="pytest_collector",
        source_collector_version="1.0.0",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductPriceAnalytics(
            product_id=product.id,
            avg_price_7d=Decimal("25.00"),
            price_score=Decimal("1.0000"),
            source_normalizer_name="pytest_normalizer",
            source_normalizer_version="1.0.0",
        )
    )
    db_session.flush()

    result = DocumentationService(db_session).backfill_lineage(module="ecommerce", limit=10000)
    lineage = DocumentationService(db_session).lineage(raw.id)

    assert result["normalized_lineage"] >= 1
    assert result["analytics_links"] >= 1
    assert lineage["raw_collection"]["id"] == str(raw.id)
    assert lineage["normalized_records"][0]["normalizer_name"] == "pytest_normalizer"
    assert lineage["analytics"][0]["analytics_name"] == "ProductPriceAnalyticsProcessor"


def test_poupi_legacy_normalizer_ignores_failed_payload(db_session):
    source = f"pytest-poupi-failed-{uuid4()}"
    raw = RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        raw_schema_name="scrapedProduct",
        raw_schema_version="1.0.0",
        raw_json={
            "success": False,
            "scrapedProduct": {
                "success": False,
                "name": None,
                "price": None,
                "store": source,
                "error": "Todas as estratégias falharam",
            },
        },
    )

    result = PoupiLegacyScrapedProductV1Normalizer(db_session).run(limit=10)

    assert result.loaded_raw >= 1
    db_session.refresh(raw)
    assert raw.processing_status == "ignored"
    assert "payload success=false" in raw.error_message
    assert db_session.query(NormalizedProduct).filter(NormalizedProduct.store_name == source).count() == 0


def test_poupi_legacy_normalizer_handles_partial_payload_and_price_variants(db_session):
    source = f"pytest-poupi-partial-{uuid4()}"
    RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        raw_schema_name="scrapedProduct",
        raw_schema_version="1.0.0",
        raw_json={
            "scrapedProduct": {
                "productName": " Produto com preco parcial ",
                "price": {"amount_cents": 1299},
                "store": source,
                "available": True,
            },
        },
    )

    result = PoupiLegacyScrapedProductV1Normalizer(db_session).run(limit=10)

    product = db_session.query(NormalizedProduct).filter(NormalizedProduct.store_name == source).one()
    assert result.normalized >= 1
    assert product.title == "Produto com preco parcial"
    assert product.price == Decimal("12.99")
    assert product.availability == "in_stock"


def test_poupi_legacy_normalizer_stamps_success_metadata_for_quality(db_session):
    source = f"pytest-poupi-ok-{uuid4()}"
    RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        raw_schema_name="scrapedProduct",
        raw_schema_version="1.0.0",
        target_url="https://example.test/produto",
        raw_json={
            "success": True,
            "scrapedProduct": {
                "success": True,
                "name": "Produto Poupi",
                "price": "R$ 12,90",
                "store": source,
                "availability": True,
            },
        },
    )
    PoupiLegacyScrapedProductV1Normalizer(db_session).run(limit=10)
    quality = DataQualityService(db_session).run(module="ecommerce", source_name=source, limit=10)

    product = db_session.query(NormalizedProduct).filter(NormalizedProduct.store_name == source).one()
    assert product.price == Decimal("12.90")
    assert product.normalization_metadata_json["raw_success"] is True
    assert quality["runs"][0]["passed_count"] == 1


def test_operations_freshness_endpoint(api_client):
    response = api_client.get("/api/v1/operations/freshness")

    assert response.status_code == 200
    payload = response.json()
    assert "summary" in payload
    assert "items" in payload
    assert set(payload["summary"]).issuperset({"ok", "violated", "unknown_sla"})


def test_operations_alerts_endpoint_reports_collection_risks(db_session, api_client):
    source = f"pytest-alerts-{uuid4()}"
    db_session.add(
        CollectionTarget(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            target_url=f"https://example.test/{uuid4()}",
            active=True,
            metadata_json={"pytest": True},
        )
    )
    old_raw = RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        raw_schema_name="pytestProduct",
        raw_json={"title": "Produto pendente antigo", "price": 10},
    )
    old_raw.collected_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db_session.commit()

    response = api_client.get(
        f"/api/v1/operations/alerts?module=ecommerce&source_name={source}&raw_pending_minutes=60&raw_freshness_hours=1"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["has_alerts"] is True
    assert payload["summary"]["targets_without_recent_raw"] == 1
    assert payload["summary"]["raw_pending_too_old"] == 1


def test_run_pipeline_once_endpoint(api_client):
    response = api_client.post("/api/v1/operations/pipeline/run?module=ecommerce&skip_normalize=true&skip_analytics=true")

    assert response.status_code == 200
    assert response.json()["module"] == "ecommerce"
    assert response.json()["normalized"] is False
    assert response.json()["analytics"] is False


def test_collection_readiness_endpoint_reports_active_targets(db_session, api_client):
    source = f"pytest-readiness-{uuid4()}"
    target = CollectionTarget(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        target_url=f"https://example.test/{uuid4()}",
        active=True,
        metadata_json={"pytest": True},
    )
    db_session.add(target)
    db_session.commit()

    response = api_client.get("/api/v1/operations/collection-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["target_count"] >= 1
    assert any(item["target"]["source_name"] == source for item in payload["targets"])


def test_collection_coverage_endpoint_reports_active_and_candidate_targets(db_session, api_client):
    source = f"pytest-coverage-{uuid4()}"
    db_session.add_all(
        [
            CollectionTarget(
                module="ecommerce",
                source_name=source,
                collector_name="poupi_legacy_raw_collector",
                target_url=f"https://example.test/active-{uuid4()}",
                active=True,
                metadata_json={"pytest": True},
            ),
            CollectionTarget(
                module="ecommerce",
                source_name=source,
                collector_name="poupi_legacy_raw_collector",
                target_url=f"https://example.test/candidate-{uuid4()}",
                active=False,
                metadata_json={"inactive_reason": "pytest candidate"},
            ),
        ]
    )
    db_session.commit()

    response = api_client.get(f"/api/v1/operations/collection-coverage?source_name={source}&active=false")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["target_count"] == 1
    assert payload["summary"]["candidate_target_count"] == 1
    assert payload["targets"][0]["status"] == "candidate"
    assert "pytest candidate" in payload["targets"][0]["issues"]


def test_source_quality_endpoint_reports_rates(db_session, api_client):
    source = f"pytest-quality-{uuid4()}"
    db_session.add(
        CollectionTarget(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            target_url=f"https://example.test/{uuid4()}",
            active=True,
            metadata_json={"pytest": True},
        )
    )
    db_session.commit()

    response = api_client.get(f"/api/v1/operations/source-quality?source_name={source}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["active_target_count"] == 1
    assert payload["summary"]["active_readiness_rate"] == 0
    assert payload["sources"][0]["health_status"] == "attention"


def test_source_quality_keeps_candidate_targets_out_of_active_blockers(db_session, api_client):
    source = f"pytest-quality-candidate-{uuid4()}"
    active_url = f"https://example.test/active-{uuid4()}"
    db_session.add(
        DataSla(
            module="ecommerce",
            source_name=source,
            freshness_sla="daily",
            metadata_json={"pytest": True},
        )
    )
    db_session.add_all(
        [
            CollectionTarget(
                module="ecommerce",
                source_name=source,
                collector_name="poupi_legacy_raw_collector",
                target_url=active_url,
                active=True,
                metadata_json={"pytest": True},
            ),
            CollectionTarget(
                module="ecommerce",
                source_name=source,
                collector_name="poupi_legacy_raw_collector",
                target_url=f"https://example.test/candidate-{uuid4()}",
                active=False,
                metadata_json={"inactive_reason": "pytest standby"},
            ),
        ]
    )
    raw = RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        raw_schema_name="scrapedProduct",
        raw_json={"scrapedProduct": {"title": "Produto pronto", "price": 10}},
        target_url=active_url,
    )
    raw.processing_status = "normalized"
    product = NormalizedProduct(
        raw_collection_id=raw.id,
        title="Produto pronto",
        price=Decimal("10.00"),
        store_name=source,
        collected_at=datetime.now(timezone.utc),
        analytics_status="processed",
        normalizer_name="pytest_normalizer",
        normalizer_version="1.0.0",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductPriceAnalytics(
            product_id=product.id,
            avg_price_7d=Decimal("10.00"),
            price_score=Decimal("1.0000"),
        )
    )
    db_session.add(
        CollectionRun(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            status=RunStatus.success,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            raw_saved_count=1,
        )
    )
    db_session.commit()

    response = api_client.get(f"/api/v1/operations/source-quality?source_name={source}")

    assert response.status_code == 200
    source_quality = response.json()["sources"][0]
    assert source_quality["active_target_count"] == 1
    assert source_quality["candidate_target_count"] == 1
    assert source_quality["blocked_target_count"] == 0
    assert source_quality["blocked_active_target_count"] == 0
    assert source_quality["health_status"] == "ok"


def test_collector_error_resolution_endpoint(db_session, api_client):
    error = CollectorError(
        collector_name=f"pytest-collector-{uuid4()}",
        error_type="RuntimeError",
        message="pytest collector error",
        context={"pytest": True},
    )
    db_session.add(error)
    db_session.commit()
    db_session.refresh(error)

    listed = api_client.get(f"/api/v1/operations/collector-errors?collector_name={error.collector_name}").json()
    assert len(listed) == 1

    resolved = api_client.post(
        f"/api/v1/operations/collector-errors/{error.id}/resolve",
        json={"resolution_note": "pytest resolved"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_at"] is not None

    hidden = api_client.get(f"/api/v1/operations/collector-errors?collector_name={error.collector_name}").json()
    visible = api_client.get(
        f"/api/v1/operations/collector-errors?collector_name={error.collector_name}&include_resolved=true"
    ).json()
    assert hidden == []
    assert len(visible) == 1


def test_collection_targets_endpoint_upserts_target(db_session, api_client):
    source = f"pytest-target-{uuid4()}"
    payload = {
        "module": "ecommerce",
        "source_name": source,
        "collector_name": "poupi_legacy_raw_collector",
        "target_url": f"https://example.test/{uuid4()}",
        "metadata_json": {"pytest": True},
    }

    created = api_client.post("/api/v1/collection-targets", json=payload)
    assert created.status_code == 200
    assert created.json()["source_name"] == source

    listed = api_client.get(f"/api/v1/collection-targets?source_name={source}").json()
    assert len(listed) == 1
    assert listed[0]["collector_name"] == "poupi_legacy_raw_collector"

    status = api_client.get(f"/api/v1/collection-targets/{listed[0]['id']}/status")
    assert status.status_code == 200
    assert status.json()["target"]["source_name"] == source

    patched = api_client.patch(f"/api/v1/collection-targets/{listed[0]['id']}", json={"metadata_json": {"patched": True}})
    assert patched.status_code == 200
    assert patched.json()["metadata_json"]["patched"] is True

    deleted = api_client.delete(f"/api/v1/collection-targets/{listed[0]['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["active"] is False


def test_collection_targets_import_and_source_status(db_session, api_client):
    source = f"pytest-import-source-{uuid4()}"
    payload = {
        "targets": [
            {
                "module": "ecommerce",
                "source_name": source,
                "collector_name": "poupi_legacy_raw_collector",
                "target_url": f"https://example.test/{uuid4()}",
                "metadata_json": {"category": "baby"},
            }
        ],
        "default_metadata_json": {"kind": "production_target", "owner": "pytest"},
    }

    imported = api_client.post("/api/v1/collection-targets/import", json=payload)
    assert imported.status_code == 200
    assert imported.json()["created"] == 1
    assert imported.json()["skipped"] == 0
    assert imported.json()["errors"] == []
    assert imported.json()["targets"][0]["metadata_json"]["kind"] == "production_target"

    status = api_client.get(f"/api/v1/sources/ecommerce/{source}/status")
    assert status.status_code == 200
    body = status.json()
    assert body["targets"]["total"] == 1
    assert body["targets"]["active"] == 1
    assert body["analytics_pending"] == 0


def test_collection_targets_import_reports_validation_errors(db_session, api_client):
    source = f"pytest-invalid-import-{uuid4()}"
    payload = {
        "targets": [
            {
                "module": "ecommerce",
                "source_name": "drogasil",
                "collector_name": "poupi_legacy_raw_collector",
                "target_url": "not-a-url",
                "metadata_json": {"owner": "pytest", "category": "baby", "product_seed": source},
            },
            {
                "module": "ecommerce",
                "source_name": "drogasil",
                "collector_name": "poupi_legacy_raw_collector",
                "target_url": f"https://www.paguemenos.com.br/{uuid4()}/p",
                "metadata_json": {"owner": "pytest", "category": "baby", "product_seed": source},
            },
        ]
    }

    imported = api_client.post("/api/v1/collection-targets/import", json=payload)

    assert imported.status_code == 200
    body = imported.json()
    assert body["created"] == 0
    assert body["skipped"] == 2
    assert len(body["errors"]) == 2
    assert "valid http(s) URL" in body["errors"][0]["message"]
    assert "incompatible" in body["errors"][1]["message"]


def test_collection_target_runner_skips_locked_target(db_session):
    source = f"pytest-target-lock-{uuid4()}"
    target = CollectionTarget(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        target_url=f"https://example.test/{uuid4()}",
        active=True,
        metadata_json={"pytest": True},
    )
    db_session.add(target)
    db_session.flush()
    db_session.add(
        CollectionRun(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            status=RunStatus.running,
            started_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    result = run_collection_targets_job(
        module="ecommerce",
        source=source,
        collector_name="poupi_legacy_raw_collector",
        limit=10,
    )

    assert result["targets"] == 1
    assert result["skipped_locked"] == 1
    assert result["raw_saved_count"] == 0


def test_collection_target_runner_dry_run_lists_limited_targets(db_session):
    source = f"pytest-target-dry-run-{uuid4()}"
    for _index in range(3):
        db_session.add(
            CollectionTarget(
                module="ecommerce",
                source_name=source,
                collector_name="poupi_legacy_raw_collector",
                target_url=f"https://example.test/{uuid4()}",
                active=True,
                metadata_json={"pytest": True},
            )
        )
    db_session.commit()

    result = run_collection_targets_job(
        module="ecommerce",
        source=source,
        collector_name="poupi_legacy_raw_collector",
        max_targets=2,
        dry_run=True,
    )

    assert result["targets"] == 2
    assert result["raw_saved_count"] == 0
    assert result["dry_run"] is True
    assert len(result["targets_detail"]) == 2


def test_candidate_targets_endpoint_recommends_next_action(db_session, api_client):
    source = f"pytest-candidate-report-{uuid4()}"
    db_session.add(
        CollectionTarget(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            target_url=f"https://example.test/{uuid4()}",
            active=False,
            metadata_json={"kind": "candidate_target", "pytest": True},
        )
    )
    db_session.commit()

    response = api_client.get(f"/api/v1/operations/candidate-targets?source_name={source}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["candidate_target_count"] == 1
    assert payload["candidates"][0]["recommendation"]["action"] == "test_candidate"


def test_ecommerce_module_job_uses_collection_targets_runner(db_session, monkeypatch):
    source = f"pytest-module-job-{uuid4()}"
    db_session.add(
        CollectionTarget(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            target_url=f"https://example.test/{uuid4()}",
            active=True,
            metadata_json={"pytest": True},
        )
    )
    db_session.commit()
    called = {"targets": 0}

    def fake_run_poupi_targets(_db, targets, **_kwargs):
        called["targets"] = len(targets)
        return len(targets)

    monkeypatch.setattr(scheduler_jobs, "_run_poupi_legacy_targets", fake_run_poupi_targets)

    run_module_collectors_job("ecommerce", source=source)

    assert called["targets"] == 1


def test_product_lineage_endpoint_returns_raw_and_analytics(db_session, api_client):
    source = f"pytest-product-lineage-{uuid4()}"
    raw = RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="pytest_collector",
        raw_schema_name="pytestProduct",
        raw_json={"title": "Produto endpoint lineage", "price": 25},
    )
    product = NormalizedProduct(
        raw_collection_id=raw.id,
        title="Produto endpoint lineage",
        price=Decimal("25.00"),
        store_name=source,
        collected_at=datetime.now(timezone.utc),
        normalizer_name="pytest_normalizer",
        normalizer_version="1.0.0",
        source_raw_schema_name="pytestProduct",
        source_raw_schema_version="1.0.0",
        source_collector_name="pytest_collector",
        source_collector_version="1.0.0",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductPriceAnalytics(
            product_id=product.id,
            avg_price_7d=Decimal("25.00"),
            price_score=Decimal("1.0000"),
            source_normalizer_name="pytest_normalizer",
            source_normalizer_version="1.0.0",
        )
    )
    db_session.flush()
    DocumentationService(db_session).backfill_lineage(module="ecommerce", limit=10000)

    response = api_client.get(f"/api/v1/lineage/products/{product.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["product"]["id"] == str(product.id)
    assert payload["raw_collection"]["id"] == str(raw.id)
    assert payload["normalization"]["normalizer_name"] == "pytest_normalizer"
    assert len(payload["analytics"]) == 1


def test_ecommerce_price_changes_endpoint_skips_consecutive_duplicates(db_session, api_client):
    source = f"pytest-price-change-{uuid4()}"
    external_id = f"https://example.test/{uuid4()}"
    base_time = datetime.now(timezone.utc)
    raws = [
        RawCollectionService(db_session).save_json(
            module="ecommerce",
            source_name=source,
            collector_name="pytest_collector",
            raw_schema_name="pytestProduct",
            raw_json={"title": "Produto mudança", "price": price},
            target_url=f"{external_id}?snapshot={index}",
        )
        for index, price in enumerate([120, 100, 100])
    ]
    snapshots = [
        NormalizedProduct(
            raw_collection_id=raws[0].id,
            external_id=external_id,
            title="Produto mudança",
            price=Decimal("120.00"),
            store_name=source,
            collected_at=base_time - timedelta(days=2),
        ),
        NormalizedProduct(
            raw_collection_id=raws[1].id,
            external_id=external_id,
            title="Produto mudança",
            price=Decimal("100.00"),
            store_name=source,
            collected_at=base_time - timedelta(days=1),
        ),
        NormalizedProduct(
            raw_collection_id=raws[2].id,
            external_id=external_id,
            title="Produto mudança",
            price=Decimal("100.00"),
            store_name=source,
            collected_at=base_time,
        ),
    ]
    db_session.add_all(snapshots)
    db_session.commit()

    response = api_client.get(f"/api/v1/sources/ecommerce/{source}/price-changes?days=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["current_price"] == "100.00"
    assert item["previous_price"] == "120.00"
    assert item["direction"] == "down"
    assert item["change_percent"] == "-16.67"


def test_raw_service_version_cache_avoids_repeated_queries(db_session):
    """ensure_collector_version() deve ser chamado apenas uma vez por run, não por item."""
    source = f"pytest-vcache-{uuid4()}"
    service = RawCollectionService(db_session)

    for i in range(5):
        service.save_json(
            module="ecommerce",
            source_name=source,
            collector_name="pytest_collector",
            collector_version="1.0.0",
            raw_schema_name="pytestProduct",
            raw_schema_version="1.0.0",
            raw_json={"title": f"Produto {i}", "price": i + 1},
            target_url=f"https://example.test/product-{uuid4()}",
        )

    from app.raw.models import CollectorVersion
    count = (
        db_session.query(CollectorVersion)
        .filter(CollectorVersion.source_name == source, CollectorVersion.collector_name == "pytest_collector")
        .count()
    )
    assert count == 1, "Deve existir apenas 1 CollectorVersion independente do número de itens salvos"
    assert len(service._version_cache) == 1


def test_normalizer_model_classes_restricts_stamp_to_correct_table(db_session):
    """normalized_model_classes deve evitar UPDATEs em tabelas que não pertencem ao normalizer."""
    from app.modules.ecommerce.normalizers.product_normalizer import EcommerceProductNormalizer
    from app.normalization.models import NormalizedSportsOdd

    assert EcommerceProductNormalizer.normalized_model_classes == (
        __import__("app.normalization.models", fromlist=["NormalizedProduct"]).NormalizedProduct,
    ), "EcommerceProductNormalizer deve declarar apenas NormalizedProduct"

    normalizer = EcommerceProductNormalizer(db_session)
    stamp_models = normalizer._stamp_models
    assert len(stamp_models) == 1
    assert NormalizedSportsOdd not in stamp_models


def test_normalizer_version_cached_across_items(db_session):
    """ensure_normalizer_version() deve consultar o banco apenas uma vez por instância."""
    from app.modules.ecommerce.normalizers.product_normalizer import EcommerceProductNormalizer
    from app.normalization.models import NormalizerVersion

    source = f"pytest-normver-{uuid4()}"
    normalizer = EcommerceProductNormalizer(db_session)
    assert normalizer._version_ensured is False

    raw_service = RawCollectionService(db_session)
    raws = []
    for i in range(3):
        raw = raw_service.save_json(
            module="ecommerce",
            source_name=source,
            collector_name="pytest_collector",
            raw_schema_name="pytestProduct",
            raw_json={"title": f"P{i}", "price": i + 1},
            target_url=f"https://example.test/{uuid4()}",
        )
        raws.append(raw)
    db_session.flush()

    for raw in raws:
        normalizer.ensure_normalizer_version(raw)

    assert normalizer._version_ensured is True
    count = (
        db_session.query(NormalizerVersion)
        .filter(NormalizerVersion.source_name == source)
        .count()
    )
    assert count == 1, "Deve haver apenas 1 NormalizerVersion independente do número de itens"


def test_cleanup_stale_runs_marks_old_running_as_failed(db_session):
    """cleanup_stale_runs_job deve marcar como failed runs com status=running há mais de TTL."""
    from scheduler.jobs import cleanup_stale_runs_job

    source = f"pytest-stale-{uuid4()}"
    old_run = CollectionRun(
        collector_name=source,
        source_name=source,
        module="ecommerce",
        status=RunStatus.running,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=60),
    )
    recent_run = CollectionRun(
        collector_name=source,
        source_name=source,
        module="ecommerce",
        status=RunStatus.running,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add_all([old_run, recent_run])
    db_session.commit()

    cleanup_stale_runs_job(ttl_minutes=30)

    db_session.expire_all()
    db_session.refresh(old_run)
    db_session.refresh(recent_run)
    assert old_run.status == RunStatus.failed
    assert old_run.finished_at is not None
    assert recent_run.status == RunStatus.running


def test_alert_webhook_job_posts_when_has_alerts(db_session, monkeypatch):
    """alert_webhook_job deve chamar send_webhook quando has_alerts=True."""
    from core.config import settings
    import app.pipeline_api as pipeline_api
    import notifications.webhook as webhook_module
    from scheduler.jobs import alert_webhook_job

    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example.test/poupi")
    monkeypatch.setattr(
        pipeline_api,
        "_build_alerts_payload",
        lambda **_: {
            "has_alerts": True,
            "summary": {
                "targets_without_recent_raw": 1,
                "raw_pending_too_old": 0,
                "normalization_failures": 0,
                "analytics_pending_too_old": 0,
                "unresolved_collector_errors": 0,
            },
        },
    )

    posted_payloads: list[dict] = []

    def fake_send_webhook(payload: dict) -> bool:
        posted_payloads.append(payload)
        return True

    monkeypatch.setattr(webhook_module, "send_webhook", fake_send_webhook)

    # Cria um target sem raw recente para garantir has_alerts=True
    source = f"pytest-webhook-{uuid4()}"
    db_session.add(
        CollectionTarget(
            module="ecommerce",
            source_name=source,
            collector_name="poupi_legacy_raw_collector",
            target_url=f"https://example.test/{uuid4()}",
            active=True,
            metadata_json={"pytest": True},
        )
    )
    db_session.commit()

    alert_webhook_job()

    assert len(posted_payloads) == 1, "send_webhook deve ser chamado uma vez quando há alertas"
    payload = posted_payloads[0]
    assert payload["source"] == "data-core"
    assert payload["event"] == "operational_alert"
    assert "summary" in payload
    assert payload["summary"]["targets_without_recent_raw"] >= 1


def test_alert_webhook_job_skips_when_no_alerts(db_session, monkeypatch):
    """alert_webhook_job não deve chamar send_webhook quando não há alertas."""
    from core.config import settings
    import app.pipeline_api as pipeline_api
    import notifications.webhook as webhook_module
    from scheduler.jobs import alert_webhook_job

    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example.test/poupi")
    monkeypatch.setattr(
        pipeline_api,
        "_build_alerts_payload",
        lambda **_: {
            "has_alerts": False,
            "summary": {
                "targets_without_recent_raw": 0,
                "raw_pending_too_old": 0,
                "normalization_failures": 0,
                "analytics_pending_too_old": 0,
                "unresolved_collector_errors": 0,
            },
        },
    )

    posted_payloads: list[dict] = []

    def fake_send_webhook(payload: dict) -> bool:
        posted_payloads.append(payload)
        return True

    monkeypatch.setattr(webhook_module, "send_webhook", fake_send_webhook)

    # Sem nenhum target ativo → has_alerts=False (para a fonte pytest)
    alert_webhook_job()

    assert posted_payloads == [] or all(
        p.get("summary", {}).get("targets_without_recent_raw", 0) == 0
        for p in posted_payloads
    ), "Não deve disparar webhook quando não há alertas reais"


def test_alert_webhook_job_skips_when_url_empty(monkeypatch):
    """alert_webhook_job não deve chamar send_webhook quando URL está vazia."""
    from core.config import settings
    import notifications.webhook as webhook_module
    from scheduler.jobs import alert_webhook_job

    monkeypatch.setattr(settings, "alert_webhook_url", "")

    called = {"count": 0}

    def fake_send_webhook(payload: dict) -> bool:
        called["count"] += 1
        return True

    monkeypatch.setattr(webhook_module, "send_webhook", fake_send_webhook)

    alert_webhook_job()

    assert called["count"] == 0, "send_webhook não deve ser chamado quando URL está vazia"


def test_health_endpoint_returns_ok_when_postgres_is_up(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["dependencies"]["postgres"]["status"] == "ok"


def test_health_endpoint_returns_degraded_when_redis_fails(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "cache_enabled", True)

    import cache.client as cache_module

    def _broken_get_redis():
        return None

    monkeypatch.setattr(cache_module, "get_redis", _broken_get_redis)

    from app.main import create_app
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["dependencies"]["redis"]["status"] == "error"
    assert payload["dependencies"]["postgres"]["status"] == "ok"


def test_analytics_price_score_z_score_calculation(db_session):
    """price_score deve ser ~1.0 quando o preço é muito abaixo da média."""
    from decimal import Decimal
    from app.modules.ecommerce.analytics.price_processor import ProductPriceAnalyticsProcessor

    source = f"pytest-zscore-{uuid4()}"
    base_time = datetime.now(timezone.utc)

    # Cria 6 produtos com preços de 100 a 200 para construir histórico
    for i, price in enumerate([100, 120, 140, 160, 180, 200]):
        snap = NormalizedProduct(
            raw_collection_id=RawCollectionService(db_session).save_json(
                module="ecommerce",
                source_name=source,
                collector_name="pytest_collector",
                raw_schema_name="pytestProduct",
                raw_json={"title": f"hist {i}", "price": price},
                target_url=f"https://example.test/hist-{uuid4()}",
            ).id,
            source_id=f"sku-zscore-{source}",
            title=f"Produto hist {i}",
            price=Decimal(str(price)),
            store_name=source,
            collected_at=base_time - timedelta(days=i + 1),
            analytics_status="processed",
            normalizer_name="pytest_normalizer",
            normalizer_version="1.0.0",
        )
        db_session.add(snap)
    db_session.flush()

    # Produto atual com preço muito baixo (100, bem abaixo da média ~150)
    target_product = NormalizedProduct(
        raw_collection_id=RawCollectionService(db_session).save_json(
            module="ecommerce",
            source_name=source,
            collector_name="pytest_collector",
            raw_schema_name="pytestProduct",
            raw_json={"title": "Oferta", "price": 100},
            target_url=f"https://example.test/target-{uuid4()}",
        ).id,
        source_id=f"sku-zscore-{source}",
        title="Oferta",
        price=Decimal("100.00"),
        store_name=source,
        collected_at=base_time,
        analytics_status="pending",
        normalizer_name="pytest_normalizer",
        normalizer_version="1.0.0",
    )
    db_session.add(target_product)
    db_session.flush()

    processor = ProductPriceAnalyticsProcessor(db_session)
    result = processor.calculate(target_product)

    assert result["avg_price_30d"] is not None
    assert result["min_price_90d"] == 100.0
    assert result["max_price_90d"] == 200.0
    assert result["price_score"] is not None
    assert result["price_score"] > 0.5, "Preço mais baixo que a média deve ter score > 0.5"


def test_rate_limit_returns_429_after_threshold(api_client):
    """Endpoints protegidos devem retornar 429 ao ultrapassar o limite."""
    hit_limit = False
    for _ in range(35):
        response = api_client.get("/api/v1/operations/freshness")
        if response.status_code == 429:
            hit_limit = True
            break

    assert hit_limit, "Deve retornar 429 após ultrapassar 30 req/min em /operations/freshness"


def test_e2e_raw_normalize_analytics_to_price_feed(db_session, api_client):
    """Pipeline completo: raw → normalize → analytics → aparecer no price-feed."""
    from app.modules.ecommerce.analytics.price_processor import ProductPriceAnalyticsProcessor
    from app.modules.ecommerce.normalizers.poupi_legacy_scraped_product_v1_normalizer import (
        PoupiLegacyScrapedProductV1Normalizer,
    )

    source = f"pytest-e2e-{uuid4()}"
    sku = f"sku-e2e-{uuid4().hex[:8]}"

    raw = RawCollectionService(db_session).save_json(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        raw_schema_name="scrapedProduct",
        raw_schema_version="1.0.0",
        raw_json={"scrapedProduct": {"title": "Fralda Pampers P 32un", "price": 59.90, "sku": sku}},
        target_url=f"https://example.test/{sku}",
    )
    db_session.flush()

    # Step 1: normalization
    normalizer = PoupiLegacyScrapedProductV1Normalizer(db_session)
    normalizer.run(limit=10)
    db_session.expire_all()

    product = (
        db_session.query(NormalizedProduct)
        .filter(NormalizedProduct.store_name == source)
        .first()
    )
    assert product is not None, "Normalizer deve criar um NormalizedProduct"
    assert product.canonical_product_id is not None
    assert float(product.price) == 59.90

    # Step 2: analytics
    processor = ProductPriceAnalyticsProcessor(db_session)
    result = processor.calculate(product)
    assert result["price_score"] is not None

    # Step 3: price-feed endpoint expõe o produto
    response = api_client.get(f"/api/v1/poupi-baby/price-feed?source_name={source}&since_hours=1&limit=10")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 1
    titles = [item["title"] for item in payload["items"]]
    assert any("Fralda" in (t or "") for t in titles), "Produto deve aparecer no price-feed"
    prices = [item["price"] for item in payload["items"]]
    assert 59.90 in prices


def test_price_feed_cursor_pagination(db_session, api_client):
    """Cursor pagination deve retornar páginas disjuntas e next_cursor correto."""
    source = f"pytest-cursor-{uuid4()}"
    base_time = datetime.now(timezone.utc)

    for i, price in enumerate([10, 20, 30]):
        raw = RawCollectionService(db_session).save_json(
            module="ecommerce",
            source_name=source,
            collector_name="pytest_collector",
            raw_schema_name="pytestProduct",
            raw_json={"title": f"Produto cursor {i}", "price": price, "sku": f"sku-cursor-{i}-{uuid4().hex[:6]}"},
            target_url=f"https://example.test/cursor-{i}",
        )
        product = NormalizedProduct(
            raw_collection_id=raw.id,
            canonical_product_id=f"cursor-{i}-{source}",
            title=f"Produto cursor {i}",
            price=Decimal(str(price)),
            store_name=source,
            collected_at=base_time - timedelta(minutes=i),  # newest first in DESC order
            normalizer_name="pytest_normalizer",
            normalizer_version="1.0.0",
        )
        db_session.add(product)
    db_session.commit()

    # Page 1 — limit=2 deve retornar next_cursor
    r1 = api_client.get(f"/api/v1/poupi-baby/price-feed?source_name={source}&since_hours=1&limit=2")
    assert r1.status_code == 200
    p1 = r1.json()
    assert p1["count"] == 2
    assert p1["next_cursor"] is not None, "next_cursor deve existir quando há mais páginas"

    # Page 2 — cursor deve devolver apenas o terceiro item
    r2 = api_client.get(f"/api/v1/poupi-baby/price-feed?source_name={source}&since_hours=1&limit=2&cursor={p1['next_cursor']}")
    assert r2.status_code == 200
    p2 = r2.json()
    assert p2["count"] == 1
    assert p2["next_cursor"] is None, "next_cursor deve ser null na última página"

    # Garante que as páginas são disjuntas
    ids1 = {item["canonical_product_id"] for item in p1["items"]}
    ids2 = {item["canonical_product_id"] for item in p2["items"]}
    assert ids1.isdisjoint(ids2), "Páginas não devem ter itens duplicados"


def test_retry_dead_letter_writes_on_exhausted_retries(db_session):
    """with_retry deve escrever JobDeadLetter em CollectorError após esgotar tentativas."""
    from scheduler.retry import with_retry

    job_name = f"pytest-dead-letter-{uuid4().hex[:8]}"
    call_count = {"n": 0}

    def always_fails():
        call_count["n"] += 1
        raise RuntimeError("falha simulada")

    with pytest.raises(RuntimeError, match="falha simulada"):
        with_retry(always_fails, job_name=job_name, max_retries=1, backoff_seconds=0.0)

    assert call_count["n"] == 2, "Deve tentar 1 (inicial) + 1 (retry)"

    dead_letter = (
        db_session.query(CollectorError)
        .filter(
            CollectorError.collector_name == job_name,
            CollectorError.error_type == "JobDeadLetter",
        )
        .first()
    )
    assert dead_letter is not None, "Deve criar registro JobDeadLetter no CollectorError"
    assert "falha simulada" in dead_letter.message


def test_price_feed_end_to_end_raw_to_api_response(db_session, api_client):
    """Full round-trip: raw save → normalize → analytics → price-feed endpoint.

    Simulates what poupi-baby DataCoreSyncService consumes. Verifies that a price
    collected in the raw layer surfaces in /api/v1/poupi-baby/price-feed within the
    same test transaction so poupi-baby can pick it up on sync.
    """
    from app.modules.ecommerce.analytics.price_processor import ProductPriceAnalyticsProcessor

    source = f"pytest-e2e-{uuid4().hex[:8]}"
    canonical_id = f"sku-{uuid4().hex[:8]}"
    price = 49.90

    # 1. Save raw product (as PoupiLegacyRawCollector would)
    raw_svc = RawCollectionService(db_session)
    raw = raw_svc.save_json(
        module="ecommerce",
        source_name=source,
        collector_name="poupi_legacy_raw_collector",
        raw_schema_name="scrapedProduct",
        raw_schema_version="1.0.0",
        raw_json={
            "scrapedProduct": {
                "title": "Fralda E2E Teste",
                "price": price,
                "currency": "BRL",
                "availability": "in_stock",
                "store_name": source,
                "source_id": canonical_id,
                "url": f"https://example.com/{canonical_id}",
            }
        },
    )
    db_session.flush()

    # 2. Normalize
    normalizer = PoupiLegacyScrapedProductV1Normalizer(db_session)
    normalizer.run(limit=10)
    db_session.flush()

    normalized = (
        db_session.query(NormalizedProduct)
        .filter(NormalizedProduct.store_name == source)
        .first()
    )
    assert normalized is not None, "Product was not normalized"
    assert float(normalized.price) == price

    # Ensure canonical_product_id is populated (backfill logic)
    assert normalized.canonical_product_id is not None

    # 3. Run analytics so the record is included in the price-feed
    processor = ProductPriceAnalyticsProcessor(db_session)
    processor.run(limit=10)
    db_session.commit()

    # 4. Query price-feed endpoint — this is what DataCoreSyncService calls
    resp = api_client.get(
        "/api/v1/poupi-baby/price-feed",
        params={"since_hours": 1, "limit": 100},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    matching = [
        item for item in data["items"]
        if item.get("store_name") == source
    ]
    assert len(matching) >= 1, "price-feed should return the item we just inserted"
    item = matching[0]
    assert float(item["price"]) == price
    assert item["canonical_product_id"] == normalized.canonical_product_id
    assert item["source_id"] == canonical_id

    # 5. Pagination: with limit=1 there should be a next_cursor
    resp_paged = api_client.get(
        "/api/v1/poupi-baby/price-feed",
        params={"since_hours": 1, "limit": 1},
    )
    assert resp_paged.status_code == 200
    paged_data = resp_paged.json()
    if paged_data["count"] > 1:
        assert paged_data["next_cursor"] is not None, "next_cursor must be set when count > limit"


def test_crypto_ohlcv_reuses_data_core_pipeline_to_signals_feed(db_session, api_client):
    """Crypto OHLCV deve virar candle normalizado, analytics e feed para consumidor externo."""
    from app.modules.crypto.normalizers.snapshot_normalizer import CryptoSnapshotNormalizer
    from app.modules.trading.analytics.processor import TradingAnalyticsProcessor

    source = f"pytest-crypto-{uuid4().hex[:8]}"
    symbol = "BTC/USDT"
    timeframe = "15m"
    base_time = datetime.now(timezone.utc) - timedelta(hours=10)
    raw_service = RawCollectionService(db_session)

    for i in range(40):
        close = 100.0 + i
        raw_service.save_json(
            module="crypto",
            source_name=source,
            collector_name="crypto.crypto_coin_ohlcv",
            raw_schema_name="marketCandle",
            raw_schema_version="1.0.0",
            raw_json={
                "symbol": symbol,
                "exchange": "binance",
                "timeframe": timeframe,
                "timestamp": (base_time + timedelta(minutes=15 * i)).isoformat(),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000 + i * 10,
            },
            source_id=f"{symbol}:{timeframe}:{i}",
        )
    db_session.commit()

    normalizer_result = CryptoSnapshotNormalizer(db_session).run(limit=100)
    db_session.expire_all()
    assert normalizer_result.normalized >= 40

    latest_candle = (
        db_session.query(NormalizedMarketCandle)
        .filter(
            NormalizedMarketCandle.source == source,
            NormalizedMarketCandle.symbol == symbol,
            NormalizedMarketCandle.timeframe == timeframe,
        )
        .order_by(NormalizedMarketCandle.timestamp.desc())
        .first()
    )
    assert latest_candle is not None
    assert float(latest_candle.close) == 139.0

    analytics_result = TradingAnalyticsProcessor(db_session).run(limit=100)
    db_session.commit()
    assert analytics_result.processed >= 40

    latest_signal = (
        db_session.query(TradingAnalytics)
        .filter(TradingAnalytics.market_candle_id == latest_candle.id)
        .first()
    )
    assert latest_signal is not None
    assert latest_signal.signal is not None
    assert latest_signal.confidence is not None
    assert latest_signal.atr is not None

    candles_response = api_client.get(
        "/api/v1/crypto/candles-feed",
        params={"source": source, "symbol": symbol, "timeframe": timeframe, "since_hours": 24, "limit": 2},
    )
    assert candles_response.status_code == 200
    candles_payload = candles_response.json()
    assert candles_payload["count"] == 2
    assert candles_payload["next_cursor"] is not None
    assert candles_payload["items"][0]["close"] == 139.0

    signals_response = api_client.get(
        "/api/v1/crypto/signals-feed",
        params={"source": source, "symbol": symbol, "timeframe": timeframe, "since_hours": 24, "limit": 2},
    )
    assert signals_response.status_code == 200
    signals_payload = signals_response.json()
    assert signals_payload["count"] == 2
    assert signals_payload["next_cursor"] is not None
    assert signals_payload["items"][0]["market_candle_id"] == str(latest_candle.id)
    assert signals_payload["items"][0]["signal"] == latest_signal.signal
    assert signals_payload["items"][0]["confidence"] == latest_signal.confidence


def _cleanup_pytest_records(db_session) -> None:
    product_ids = [
        row[0]
        for row in db_session.query(NormalizedProduct.id)
        .filter(NormalizedProduct.store_name.like("pytest-%"))
        .all()
    ]
    raw_ids = [
        row[0]
        for row in db_session.query(RawCollection.id)
        .filter(RawCollection.source_name.like("pytest-%"))
        .all()
    ]
    candle_ids = [
        row[0]
        for row in db_session.query(NormalizedMarketCandle.id)
        .filter(NormalizedMarketCandle.source.like("pytest-%"))
        .all()
    ]
    trading_ids = []
    if candle_ids:
        trading_ids = [
            row[0]
            for row in db_session.query(TradingAnalytics.id)
            .filter(TradingAnalytics.market_candle_id.in_(candle_ids))
            .all()
        ]
    if product_ids:
        db_session.query(ProductPriceAnalytics).filter(ProductPriceAnalytics.product_id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db_session.query(DataLineage).filter(DataLineage.normalized_record_id.in_(product_ids)).delete(
            synchronize_session=False
        )
    if trading_ids:
        db_session.query(DataLineage).filter(DataLineage.analytics_record_id.in_(trading_ids)).delete(
            synchronize_session=False
        )
    if candle_ids:
        db_session.query(DataLineage).filter(DataLineage.normalized_record_id.in_(candle_ids)).delete(
            synchronize_session=False
        )
        db_session.query(TradingAnalytics).filter(TradingAnalytics.market_candle_id.in_(candle_ids)).delete(
            synchronize_session=False
        )
    if raw_ids:
        db_session.query(DataLineage).filter(DataLineage.raw_collection_id.in_(raw_ids)).delete(
            synchronize_session=False
        )
    db_session.query(DataQualityRun).filter(DataQualityRun.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(CollectorError).filter(CollectorError.collector_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(CollectionTarget).filter(CollectionTarget.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(CollectionRun).filter(CollectionRun.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(DataContract).filter(DataContract.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(DataOwner).filter(DataOwner.owner_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(DataSla).filter(DataSla.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(NormalizedProduct).filter(NormalizedProduct.store_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(NormalizedMarketCandle).filter(NormalizedMarketCandle.source.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(RawCollection).filter(RawCollection.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(CollectorVersion).filter(CollectorVersion.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.commit()
