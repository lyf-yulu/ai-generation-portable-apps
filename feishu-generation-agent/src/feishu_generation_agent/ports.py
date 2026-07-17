from typing import Protocol

from feishu_generation_agent.domain.artifact import (
    Artifact,
    DeliveryRecord,
    ProviderSubmission,
)
from feishu_generation_agent.domain.document import (
    MediaAsset,
    NormalizedDocument,
    RequirementRequest,
    VisionDescription,
)
from feishu_generation_agent.domain.plan import (
    AuditReport,
    GenerationTask,
    TaskPlan,
)


class DocumentSource(Protocol):
    async def ingest(self, request: RequirementRequest) -> NormalizedDocument:
        raise NotImplementedError

    async def get_revision(self, source_url: str) -> int:
        raise NotImplementedError


class VisionAnalyzer(Protocol):
    async def analyze(self, asset: MediaAsset) -> VisionDescription:
        raise NotImplementedError


class RequirementPlanner(Protocol):
    async def plan(
        self,
        document: NormalizedDocument,
        descriptions: list[VisionDescription],
        feedback: str | None,
    ) -> TaskPlan:
        raise NotImplementedError

    async def audit(
        self,
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> AuditReport:
        raise NotImplementedError


class ImageGenerator(Protocol):
    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        raise NotImplementedError

    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        raise NotImplementedError


class VideoGenerator(Protocol):
    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        raise NotImplementedError

    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        raise NotImplementedError


class DeliveryWriter(Protocol):
    async def deliver(
        self,
        document: NormalizedDocument,
        plan: TaskPlan,
        artifacts: list[Artifact],
    ) -> DeliveryRecord:
        raise NotImplementedError
