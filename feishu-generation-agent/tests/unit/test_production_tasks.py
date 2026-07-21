from pathlib import Path

import pytest

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
    ResultTableTarget,
)
from feishu_generation_agent.storage.production_tasks import (
    ProductionTaskAlreadyClaimed,
    ProductionTaskStore,
)


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiProd",
        app_token="appProd",
        table_id="tblProd",
        view_id="vewProd",
        source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
    )


def _task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="recProd",
        display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/docA",
        progress="未开始",
        maker_open_id="ou-maker",
        maker_name="制作人",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A",
            requirement_attachment="https://tenant.feishu.cn/docx/docA",
            project_names=["项目 A"],
            requester_open_ids=["ou-requester"],
            requester_names=["发起人"],
            maker_open_ids=["ou-maker"],
            maker_names=["制作人"],
        ),
    )


async def test_claim_is_unique_per_source_record_and_persists_snapshot(
    tmp_path: Path,
) -> None:
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        binding = await store.claim(
            _location(), _task(), run_id="run-1", thread_id="thread-1"
        )

        assert binding.snapshot.requirement_name == "需求 A"
        with pytest.raises(ProductionTaskAlreadyClaimed):
            await store.claim(
                _location(), _task(), run_id="run-2", thread_id="thread-2"
            )
    finally:
        await store.close()


async def test_result_target_and_delivery_row_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(path)
    try:
        await store.claim(_location(), _task(), run_id="run-1", thread_id="thread-1")
        await store.upsert_result_target(
            ResultTableTarget(
                maker_open_id="ou-maker",
                maker_name="制作人",
                app_token="app-result",
                table_id="tbl-result",
                url="https://tenant.feishu.cn/base/app-result",
            )
        )
        await store.reserve_delivery("run-1")
        await store.complete_delivery("run-1", result_record_id="rec-result")
    finally:
        await store.close()

    reopened = await ProductionTaskStore.open(path)
    try:
        assert (await reopened.get_result_target("ou-maker")).table_id == "tbl-result"
        assert (await reopened.get_delivery("run-1")).result_record_id == "rec-result"
    finally:
        await reopened.close()
