from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics.models import (
    CryptoAnalytics,
    ProductPriceAnalytics,
    RealEstateAnalytics,
    SportsOddsAnalytics,
    TradingAnalytics,
)
from app.documentation.generators import DocumentationGenerator
from app.documentation.lineage import LineageService
from app.documentation.models import (
    AnalyticsDocumentation,
    CollectorDocumentation,
    DataContract,
    DataOwner,
    DataSla,
    EntityRelationship,
    NormalizerDocumentation,
    SchemaDocumentation,
)
from app.normalization.models import (
    NormalizedCryptoSnapshot,
    NormalizedMarketCandle,
    NormalizedProduct,
    NormalizedRealEstateListing,
    NormalizedSportsOdd,
)
from app.raw.models import RawCollection


class DocumentationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def ensure_defaults(self) -> dict[str, int]:
        try:
            result = DocumentationGenerator(self.db).seed_defaults()
            self.ensure_governance_defaults()
            self.db.commit()
            return result
        except IntegrityError:
            self.db.rollback()
            return {"schemas": 0, "tables": 0, "relationships": 0, "collectors": 0, "normalizers": 0, "analytics": 0}

    def ensure_governance_defaults(self) -> None:
        for module_name in _pipeline_modules():
            owner = (
                self.db.query(DataOwner)
                .filter(DataOwner.module == module_name, DataOwner.owner_name == "data-platform")
                .one_or_none()
            )
            if owner is None:
                self.db.add(
                    DataOwner(
                        module=module_name,
                        owner_name="data-platform",
                        description=f"Default technical owner for the {module_name} data domain.",
                        metadata_json={"seeded": True},
                    )
                )
            sla = self.db.query(DataSla).filter(DataSla.module == module_name, DataSla.source_name.is_(None)).one_or_none()
            if sla is None:
                self.db.add(
                    DataSla(
                        module=module_name,
                        freshness_sla="not_defined",
                        availability_sla="not_defined",
                        quality_sla="not_defined",
                        metadata_json={"seeded": True},
                    )
                )
            contract = (
                self.db.query(DataContract)
                .filter(
                    DataContract.module == module_name,
                    DataContract.source_name.is_(None),
                    DataContract.contract_name == f"{module_name}_pipeline_contract",
                    DataContract.contract_version == "1.0.0",
                )
                .one_or_none()
            )
            if contract is None:
                self.db.add(
                    DataContract(
                        module=module_name,
                        contract_name=f"{module_name}_pipeline_contract",
                        contract_version="1.0.0",
                        owner_name="data-platform",
                        freshness_sla="not_defined",
                        criticality="medium",
                        status="draft",
                        raw_required=True,
                        lineage_required=True,
                        quality_required=True,
                        schema_rules_json={"raw": "all collected payloads must be saved before normalization"},
                        quality_rules_json={"minimum": "domain-specific required fields must be present"},
                        metadata_json={"seeded": True},
                    )
                )
        for sla_default in _source_sla_defaults():
            source_sla = (
                self.db.query(DataSla)
                .filter(
                    DataSla.module == sla_default["module"],
                    DataSla.source_name == sla_default["source_name"],
                )
                .one_or_none()
            )
            if source_sla is None:
                self.db.add(
                    DataSla(
                        module=sla_default["module"],
                        source_name=sla_default["source_name"],
                        freshness_sla=sla_default["freshness_sla"],
                        availability_sla=sla_default.get("availability_sla", "not_defined"),
                        quality_sla=sla_default.get("quality_sla", "not_defined"),
                        metadata_json={
                            "seeded": True,
                            "reason": "initial operational freshness default",
                        },
                    )
                )
        self.db.flush()

    def schemas(
        self,
        *,
        name: str | None = None,
        module: str | None = None,
        schema_type: str | None = None,
    ) -> list[SchemaDocumentation]:
        query = self.db.query(SchemaDocumentation)
        if name:
            query = query.filter(SchemaDocumentation.schema_name == name)
        if module:
            query = query.filter(SchemaDocumentation.module == module)
        if schema_type:
            query = query.filter(SchemaDocumentation.schema_type == schema_type)
        return query.order_by(SchemaDocumentation.module, SchemaDocumentation.schema_name).all()

    def lineage(self, raw_collection_id: UUID) -> dict[str, Any]:
        return LineageService(self.db).lineage_for_raw(raw_collection_id)

    def relationships(self, *, module: str | None = None) -> list[EntityRelationship]:
        query = self.db.query(EntityRelationship)
        if module:
            query = query.filter(EntityRelationship.module == module)
        return query.order_by(EntityRelationship.module, EntityRelationship.source_entity).all()

    def collectors(self, *, module: str | None = None, source_name: str | None = None) -> list[CollectorDocumentation]:
        query = self.db.query(CollectorDocumentation)
        if module:
            query = query.filter(CollectorDocumentation.module == module)
        if source_name:
            query = query.filter(CollectorDocumentation.source_name == source_name)
        return query.order_by(CollectorDocumentation.module, CollectorDocumentation.collector_name).all()

    def normalizers(self, *, module: str | None = None) -> list[NormalizerDocumentation]:
        query = self.db.query(NormalizerDocumentation)
        if module:
            query = query.filter(NormalizerDocumentation.module == module)
        return query.order_by(NormalizerDocumentation.module, NormalizerDocumentation.normalizer_name).all()

    def analytics(self, *, module: str | None = None) -> list[AnalyticsDocumentation]:
        query = self.db.query(AnalyticsDocumentation)
        if module:
            query = query.filter(AnalyticsDocumentation.module == module)
        return query.order_by(AnalyticsDocumentation.module, AnalyticsDocumentation.analytics_name).all()

    def catalog(self, *, module: str | None = None) -> dict[str, Any]:
        self.ensure_defaults()
        raw_counts = self._count_raw_by_module()
        normalized_counts = self._count_models(self._normalized_models())
        analytics_counts = self._count_models(self._analytics_models_for_counts())
        if module:
            raw_counts = {module: raw_counts.get(module, 0)}
            normalized_counts = {module: normalized_counts.get(module, 0)}
            analytics_counts = {module: analytics_counts.get(module, 0)}
        return {
            "entities": {"raw": raw_counts, "normalized": normalized_counts, "analytics": analytics_counts},
            "schemas": len(self.schemas(module=module)),
            "relationships": len(self.relationships(module=module)),
            "collectors": len(self.collectors(module=module)),
            "normalizers": len(self.normalizers(module=module)),
            "analytics_processors": len(self.analytics(module=module)),
            "coverage": self.coverage(module=module),
            "data_contracts": self.data_contracts(module=module),
            "owners": self.owners(module=module),
        }

    def coverage(
        self,
        *,
        module: str | None = None,
        source_name: str | None = None,
        raw_schema_name: str | None = None,
        collector_version: str | None = None,
        normalizer_version: str | None = None,
    ) -> dict[str, Any]:
        query = self.db.query(
            RawCollection.module,
            RawCollection.source_name,
            RawCollection.collector_version,
            RawCollection.raw_schema_name,
            RawCollection.raw_schema_version,
            RawCollection.processing_status,
            func.count(RawCollection.id),
        )
        if module:
            query = query.filter(RawCollection.module == module)
        if source_name:
            query = query.filter(RawCollection.source_name == source_name)
        if raw_schema_name:
            query = query.filter(RawCollection.raw_schema_name == raw_schema_name)
        if collector_version:
            query = query.filter(RawCollection.collector_version == collector_version)
        rows = (
            query.group_by(
                RawCollection.module,
                RawCollection.source_name,
                RawCollection.collector_version,
                RawCollection.raw_schema_name,
                RawCollection.raw_schema_version,
                RawCollection.processing_status,
            )
            .all()
        )
        grouped: dict[str, dict[str, Any]] = {}
        for module_name, source, collector_ver, schema, version, status, count in rows:
            key = f"{module_name}:{source}:{collector_ver}:{schema}:{version}"
            item = grouped.setdefault(
                key,
                {
                    "module": module_name,
                    "source_name": source,
                    "collector_version": collector_ver,
                    "raw_schema_name": schema,
                    "raw_schema_version": version,
                    "raw_total": 0,
                    "statuses": {},
                },
            )
            item["raw_total"] += count
            item["statuses"][status] = count
        for item in grouped.values():
            normalized = item["statuses"].get("normalized", 0)
            item["normalization_coverage"] = normalized / item["raw_total"] if item["raw_total"] else 0
            item["normalization_failed"] = item["statuses"].get("normalization_failed", 0)
            item["normalization_pending"] = item["statuses"].get("normalization_pending", 0)
            item["field_coverage"] = self.field_coverage(
                module=item["module"],
                source_name=item["source_name"],
                normalizer_version=normalizer_version,
            )
        return {"items": sorted(grouped.values(), key=lambda row: (row["module"], row["source_name"]))}

    def field_coverage(
        self,
        *,
        module: str,
        source_name: str | None = None,
        normalizer_version: str | None = None,
    ) -> dict[str, Any]:
        model, fields = self._quality_model_and_fields(module)
        if model is None:
            return {}
        query = self.db.query(model)
        if source_name and hasattr(model, "store_name"):
            query = query.filter(model.store_name == source_name)
        if normalizer_version and hasattr(model, "normalizer_version"):
            query = query.filter(model.normalizer_version == normalizer_version)
        records = query.limit(1000).all()
        total = len(records)
        coverage = {}
        for field in fields:
            present = sum(1 for record in records if getattr(record, field, None) not in (None, ""))
            coverage[field] = {"present": present, "total": total, "ratio": present / total if total else 0}
        return coverage

    def erd(self, *, module: str | None = None) -> dict[str, Any]:
        self.ensure_defaults()
        table_docs = self.schemas(module=module, schema_type="table")
        relationships = self.relationships(module=module)
        nodes = {
            row.schema_name: {
                "id": row.schema_name,
                "module": row.module,
                "type": "table",
                "field_count": len(row.fields_json or {}),
            }
            for row in table_docs
        }
        edges = []
        for relationship in relationships:
            nodes.setdefault(relationship.source_entity, {"id": relationship.source_entity, "module": relationship.module, "type": "entity"})
            nodes.setdefault(relationship.target_entity, {"id": relationship.target_entity, "module": relationship.module, "type": "entity"})
            edges.append(
                {
                    "source": relationship.source_entity,
                    "target": relationship.target_entity,
                    "relationship_type": relationship.relationship_type,
                    "description": relationship.description,
                    "module": relationship.module,
                }
            )
        for table in table_docs:
            for foreign_key in (table.relationships_json or {}).get("foreign_keys", []):
                for target in foreign_key.get("references", []):
                    target_table = target.split(".")[0]
                    nodes.setdefault(target_table, {"id": target_table, "module": "unknown", "type": "table"})
                    edges.append(
                        {
                            "source": table.schema_name,
                            "target": target_table,
                            "relationship_type": "foreign_key",
                            "description": f"{table.schema_name}.{foreign_key['column']} references {target}",
                            "module": table.module,
                        }
                    )
        return {"nodes": sorted(nodes.values(), key=lambda node: node["id"]), "edges": edges}

    def openapi_extension(self, *, module: str | None = None) -> dict[str, Any]:
        return {
            "x-data-core-catalog": self.catalog(module=module),
            "x-data-core-relationships": [_model_to_dict(row) for row in self.relationships(module=module)],
            "x-data-core-schemas": [_model_to_dict(row) for row in self.schemas(module=module)],
            "x-data-core-lineage-contract": {
                "flow": ["collector", "raw_collection", "normalizer", "normalized_record", "data_quality", "analytics"],
                "required_trace_fields": [
                    "collector_name",
                    "collector_version",
                    "raw_schema_name",
                    "raw_schema_version",
                    "normalizer_name",
                    "normalizer_version",
                    "analytics_processor_name",
                    "analytics_processor_version",
                ],
            },
        }

    def data_contracts(self, *, module: str | None = None) -> list[dict[str, Any]]:
        self.ensure_governance_defaults()
        query = self.db.query(DataContract).filter(DataContract.status != "disabled")
        if module:
            query = query.filter(DataContract.module == module)
        contracts = query.order_by(DataContract.module, DataContract.source_name, DataContract.contract_name).all()
        return [_contract_to_dict(item) for item in contracts]

    def owners(self, *, module: str | None = None) -> list[dict[str, Any]]:
        self.ensure_governance_defaults()
        query = self.db.query(DataOwner).filter(DataOwner.is_active.is_(True))
        if module:
            query = query.filter(DataOwner.module == module)
        owners = query.order_by(DataOwner.module, DataOwner.owner_name).all()
        slas = {item["module"]: item for item in self.slas(module=module) if item.get("source_name") is None}
        return [
            {
                "id": str(item.id),
                "module": item.module,
                "owner": item.owner_name,
                "technical_contact": item.technical_contact,
                "business_contact": item.business_contact,
                "sla": slas.get(item.module, {}).get("freshness_sla", "not_defined"),
                "description": item.description,
                "metadata": item.metadata_json,
            }
            for item in owners
        ]

    def slas(self, *, module: str | None = None) -> list[dict[str, Any]]:
        self.ensure_governance_defaults()
        query = self.db.query(DataSla).filter(DataSla.is_active.is_(True))
        if module:
            query = query.filter(DataSla.module == module)
        return [
            {
                "id": str(item.id),
                "module": item.module,
                "source_name": item.source_name,
                "freshness_sla": item.freshness_sla,
                "availability_sla": item.availability_sla,
                "quality_sla": item.quality_sla,
                "metadata": item.metadata_json,
            }
            for item in query.order_by(DataSla.module, DataSla.source_name).all()
        ]

    def upsert_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        module = payload["module"]
        source_name = payload.get("source_name")
        contract_name = payload.get("contract_name") or f"{module}_pipeline_contract"
        contract_version = payload.get("contract_version") or "1.0.0"
        contract = (
            self.db.query(DataContract)
            .filter(
                DataContract.module == module,
                DataContract.source_name.is_(None) if source_name is None else DataContract.source_name == source_name,
                DataContract.contract_name == contract_name,
                DataContract.contract_version == contract_version,
            )
            .one_or_none()
        )
        if contract is None:
            contract = DataContract(
                module=module,
                source_name=source_name,
                contract_name=contract_name,
                contract_version=contract_version,
                owner_name=payload.get("owner_name") or "data-platform",
                freshness_sla=payload.get("freshness_sla") or "not_defined",
                criticality=payload.get("criticality") or "medium",
                status=payload.get("status") or "draft",
                raw_required=payload.get("raw_required", True),
                lineage_required=payload.get("lineage_required", True),
                quality_required=payload.get("quality_required", True),
                schema_rules_json=payload.get("schema_rules_json") or {},
                quality_rules_json=payload.get("quality_rules_json") or {},
                metadata_json=payload.get("metadata_json") or {},
            )
            self.db.add(contract)
        else:
            _apply_updates(
                contract,
                payload,
                {
                    "owner_name",
                    "freshness_sla",
                    "criticality",
                    "status",
                    "raw_required",
                    "lineage_required",
                    "quality_required",
                    "schema_rules_json",
                    "quality_rules_json",
                    "metadata_json",
                },
            )
        self.db.commit()
        self.db.refresh(contract)
        return _contract_to_dict(contract)

    def update_contract(self, contract_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        contract = self.db.get(DataContract, contract_id)
        if contract is None:
            return None
        _apply_updates(
            contract,
            payload,
            {
                "source_name",
                "contract_name",
                "contract_version",
                "owner_name",
                "freshness_sla",
                "criticality",
                "status",
                "raw_required",
                "lineage_required",
                "quality_required",
                "schema_rules_json",
                "quality_rules_json",
                "metadata_json",
            },
        )
        self.db.commit()
        self.db.refresh(contract)
        return _contract_to_dict(contract)

    def delete_contract(self, contract_id: UUID) -> bool:
        contract = self.db.get(DataContract, contract_id)
        if contract is None:
            return False
        contract.status = "disabled"
        self.db.commit()
        return True

    def upsert_owner(self, payload: dict[str, Any]) -> dict[str, Any]:
        owner = (
            self.db.query(DataOwner)
            .filter(DataOwner.module == payload["module"], DataOwner.owner_name == payload["owner_name"])
            .one_or_none()
        )
        if owner is None:
            owner = DataOwner(
                module=payload["module"],
                owner_name=payload["owner_name"],
                technical_contact=payload.get("technical_contact"),
                business_contact=payload.get("business_contact"),
                description=payload.get("description"),
                is_active=payload.get("is_active", True),
                metadata_json=payload.get("metadata_json") or {},
            )
            self.db.add(owner)
        else:
            _apply_updates(
                owner,
                payload,
                {"technical_contact", "business_contact", "description", "is_active", "metadata_json"},
            )
        self.db.commit()
        self.db.refresh(owner)
        return _owner_to_dict(owner)

    def update_owner(self, owner_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        owner = self.db.get(DataOwner, owner_id)
        if owner is None:
            return None
        _apply_updates(
            owner,
            payload,
            {"owner_name", "technical_contact", "business_contact", "description", "is_active", "metadata_json"},
        )
        self.db.commit()
        self.db.refresh(owner)
        return _owner_to_dict(owner)

    def delete_owner(self, owner_id: UUID) -> bool:
        owner = self.db.get(DataOwner, owner_id)
        if owner is None:
            return False
        owner.is_active = False
        self.db.commit()
        return True

    def upsert_sla(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_name = payload.get("source_name")
        sla = (
            self.db.query(DataSla)
            .filter(
                DataSla.module == payload["module"],
                DataSla.source_name.is_(None) if source_name is None else DataSla.source_name == source_name,
            )
            .one_or_none()
        )
        if sla is None:
            sla = DataSla(
                module=payload["module"],
                source_name=source_name,
                freshness_sla=payload.get("freshness_sla") or "not_defined",
                availability_sla=payload.get("availability_sla"),
                quality_sla=payload.get("quality_sla"),
                is_active=payload.get("is_active", True),
                metadata_json=payload.get("metadata_json") or {},
            )
            self.db.add(sla)
        else:
            _apply_updates(
                sla,
                payload,
                {"freshness_sla", "availability_sla", "quality_sla", "is_active", "metadata_json"},
            )
        self.db.commit()
        self.db.refresh(sla)
        return _sla_to_dict(sla)

    def update_sla(self, sla_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        sla = self.db.get(DataSla, sla_id)
        if sla is None:
            return None
        _apply_updates(
            sla,
            payload,
            {"source_name", "freshness_sla", "availability_sla", "quality_sla", "is_active", "metadata_json"},
        )
        self.db.commit()
        self.db.refresh(sla)
        return _sla_to_dict(sla)

    def delete_sla(self, sla_id: UUID) -> bool:
        sla = self.db.get(DataSla, sla_id)
        if sla is None:
            return False
        sla.is_active = False
        self.db.commit()
        return True

    def backfill_lineage(self, *, module: str | None = None, limit: int = 500) -> dict[str, int]:
        service = LineageService(self.db)
        normalized_models = self._normalized_models()
        analytics_models = {
            "ecommerce": (ProductPriceAnalytics, "product_price_analytics", "product_id"),
            "real_estate": (RealEstateAnalytics, "real_estate_analytics", "listing_id"),
            "crypto": (CryptoAnalytics, "crypto_analytics", None),
            "trading": (TradingAnalytics, "trading_analytics", None),
            "sports_odds": (SportsOddsAnalytics, "sports_odds_analytics", None),
        }
        created_or_seen = 0
        analytics_attached = 0
        modules = [module] if module else list(normalized_models.keys())
        for module_name in modules:
            model = normalized_models[module_name]
            order_column = model.collected_at if hasattr(model, "collected_at") else model.id
            records = self.db.query(model).order_by(order_column).limit(limit).all()
            for record in records:
                raw = self.db.get(RawCollection, record.raw_collection_id)
                if not raw:
                    continue
                normalizer_name = getattr(record, "normalizer_name", None) or "unknown"
                normalizer_version = getattr(record, "normalizer_version", None) or "unknown"
                service.record_normalized(
                    raw=raw,
                    normalizer_name=normalizer_name,
                    normalizer_version=normalizer_version,
                    normalized_record_type=model.__tablename__,
                    normalized_record_id=record.id,
                    metadata={"backfilled": True},
                )
                created_or_seen += 1
                analytics_model, analytics_type, fk = analytics_models[module_name]
                for analytics_row in self._analytics_for_record(module_name, analytics_model, fk, record):
                    analytics_attached += service.attach_analytics(
                        normalized_record_type=model.__tablename__,
                        normalized_record_id=record.id,
                        analytics_processor_name=self._analytics_processor_name(module_name),
                        analytics_processor_version=getattr(analytics_row, "source_normalizer_version", None) or "1.0.0",
                        analytics_record_type=analytics_type,
                        analytics_record_id=analytics_row.id,
                        metadata={"backfilled": True},
                    )
        self.db.commit()
        return {"normalized_lineage": created_or_seen, "analytics_links": analytics_attached}

    def _analytics_for_record(self, module: str, analytics_model: type, fk: str | None, record: object) -> list[object]:
        if fk:
            return self.db.query(analytics_model).filter(getattr(analytics_model, fk) == record.id).all()
        if module == "crypto":
            return self.db.query(analytics_model).filter(analytics_model.symbol == record.symbol).all()
        if module == "trading":
            return (
                self.db.query(analytics_model)
                .filter(analytics_model.symbol == record.symbol, analytics_model.timeframe == record.timeframe)
                .all()
            )
        if module == "sports_odds":
            return (
                self.db.query(analytics_model)
                .filter(
                    analytics_model.event_id == record.event_external_id,
                    analytics_model.market_type == record.market_type,
                    analytics_model.selection == record.selection,
                )
                .all()
            )
        return []

    @staticmethod
    def _analytics_processor_name(module: str) -> str:
        return {
            "ecommerce": "ProductPriceAnalyticsProcessor",
            "real_estate": "RealEstateAnalyticsProcessor",
            "crypto": "CryptoAnalyticsProcessor",
            "trading": "TradingAnalyticsProcessor",
            "sports_odds": "SportsOddsAnalyticsProcessor",
        }.get(module, "unknown")

    @staticmethod
    def _normalized_models() -> dict[str, type]:
        return {
            "ecommerce": NormalizedProduct,
            "real_estate": NormalizedRealEstateListing,
            "crypto": NormalizedCryptoSnapshot,
            "trading": NormalizedMarketCandle,
            "sports_odds": NormalizedSportsOdd,
        }

    @staticmethod
    def _analytics_models_for_counts() -> dict[str, type]:
        return {
            "ecommerce": ProductPriceAnalytics,
            "real_estate": RealEstateAnalytics,
            "crypto": CryptoAnalytics,
            "trading": TradingAnalytics,
            "sports_odds": SportsOddsAnalytics,
        }

    @staticmethod
    def _quality_model_and_fields(module: str) -> tuple[type | None, list[str]]:
        return {
            "ecommerce": (NormalizedProduct, ["title", "price", "availability", "store_name"]),
            "real_estate": (NormalizedRealEstateListing, ["title", "price", "city", "neighborhood", "area_m2"]),
            "crypto": (NormalizedCryptoSnapshot, ["symbol", "price", "volume"]),
            "trading": (NormalizedMarketCandle, ["symbol", "timeframe", "open", "high", "low", "close", "volume"]),
            "sports_odds": (NormalizedSportsOdd, ["sportsbook", "sport", "league", "event_external_id", "market_type", "selection", "odd"]),
        }.get(module, (None, []))

    def _count_raw_by_module(self) -> dict[str, int]:
        return {
            module: count
            for module, count in self.db.query(RawCollection.module, func.count(RawCollection.id)).group_by(RawCollection.module).all()
        }

    def _count_models(self, models: dict[str, type]) -> dict[str, int]:
        return {module: self.db.query(model).count() for module, model in models.items()}


def _model_to_dict(row: object) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _contract_to_dict(item: DataContract) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "module": item.module,
        "source_name": item.source_name,
        "contract_name": item.contract_name,
        "contract_version": item.contract_version,
        "owner": item.owner_name,
        "freshness_sla": item.freshness_sla,
        "criticality": item.criticality,
        "contract_status": item.status,
        "raw_required": item.raw_required,
        "lineage_required": item.lineage_required,
        "quality_required": item.quality_required,
        "schema_rules": item.schema_rules_json,
        "quality_rules": item.quality_rules_json,
        "metadata": item.metadata_json,
    }


def _owner_to_dict(item: DataOwner) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "module": item.module,
        "owner": item.owner_name,
        "technical_contact": item.technical_contact,
        "business_contact": item.business_contact,
        "description": item.description,
        "is_active": item.is_active,
        "metadata": item.metadata_json,
    }


def _sla_to_dict(item: DataSla) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "module": item.module,
        "source_name": item.source_name,
        "freshness_sla": item.freshness_sla,
        "availability_sla": item.availability_sla,
        "quality_sla": item.quality_sla,
        "is_active": item.is_active,
        "metadata": item.metadata_json,
    }


def _apply_updates(row: object, payload: dict[str, Any], fields: set[str]) -> None:
    for field in fields:
        if field in payload:
            setattr(row, field, payload[field])


def _pipeline_modules() -> list[str]:
    return ["ecommerce", "real_estate", "crypto", "trading", "sports_odds"]


def _source_sla_defaults() -> list[dict[str, str]]:
    return [
        {"module": "ecommerce", "source_name": "drogasil", "freshness_sla": "daily", "quality_sla": "0.95"},
        {"module": "ecommerce", "source_name": "drogaraia", "freshness_sla": "daily", "quality_sla": "0.95"},
        {"module": "ecommerce", "source_name": "paguemenos", "freshness_sla": "daily", "quality_sla": "0.95"},
        {"module": "ecommerce", "source_name": "mercadolivre", "freshness_sla": "daily", "quality_sla": "0.95"},
        {"module": "ecommerce", "source_name": "poupi_legacy", "freshness_sla": "daily", "quality_sla": "0.95"},
        {"module": "ecommerce", "source_name": "generic_marketplace", "freshness_sla": "daily", "quality_sla": "0.95"},
        {"module": "real_estate", "source_name": "generic_real_estate", "freshness_sla": "daily", "quality_sla": "0.90"},
        {"module": "crypto", "source_name": "crypto_coin_exchange", "freshness_sla": "1h", "quality_sla": "0.98"},
        {"module": "crypto", "source_name": "generic_exchange", "freshness_sla": "1h", "quality_sla": "0.95"},
        {"module": "sports_odds", "source_name": "generic_bookmaker", "freshness_sla": "15m", "quality_sla": "0.95"},
    ]
