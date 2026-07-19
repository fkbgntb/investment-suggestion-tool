"""Framework-independent, versioned domain models and module contracts."""

from app.domain.analysis import AnalysisResult, DecisionContext, DecisionResult, Report
from app.domain.contracts import (
    AIProvider,
    DecisionPolicy,
    MarketDataProvider,
    NotificationProvider,
    ReportRenderer,
    SourceAdapter,
    StorageProvider,
    TaskDispatcher,
)
from app.domain.documents import EventCluster, RawDocument
from app.domain.evidence import Evidence, EvidenceScore
from app.domain.jobs import JobRun
from app.domain.portfolio import Asset, InvestmentProfile, Position
from app.domain.taxonomy import Entity, Exposure, Source, Topic

__all__ = [
    "AIProvider",
    "AnalysisResult",
    "Asset",
    "DecisionContext",
    "DecisionPolicy",
    "DecisionResult",
    "Entity",
    "EventCluster",
    "Evidence",
    "EvidenceScore",
    "Exposure",
    "InvestmentProfile",
    "JobRun",
    "MarketDataProvider",
    "NotificationProvider",
    "Position",
    "RawDocument",
    "Report",
    "ReportRenderer",
    "Source",
    "SourceAdapter",
    "StorageProvider",
    "TaskDispatcher",
    "Topic",
]
