from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.analytics.models import ProductPriceAnalytics
from app.data_quality.models import DataQualityRun
from app.data_quality.services import DataQualityService
from app.documentation.models import DataContract, DataLineage, DataOwner, DataSla
from app.documentation.services import DocumentationService
from app.modules.ecommerce.normalizers.poupi_legacy_scraped_product_v1_normalizer import PoupiLegacyScrapedProductV1Normalizer
from app.normalization.models import NormalizedProduct
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
    assert db_session.query(NormalizedProduct).filter(NormalizedProduct.store_name == source).count() == 0


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

    def fake_run_poupi_targets(_db, targets):
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
    if product_ids:
        db_session.query(ProductPriceAnalytics).filter(ProductPriceAnalytics.product_id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db_session.query(DataLineage).filter(DataLineage.normalized_record_id.in_(product_ids)).delete(
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
    db_session.query(RawCollection).filter(RawCollection.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.query(CollectorVersion).filter(CollectorVersion.source_name.like("pytest-%")).delete(synchronize_session=False)
    db_session.commit()
