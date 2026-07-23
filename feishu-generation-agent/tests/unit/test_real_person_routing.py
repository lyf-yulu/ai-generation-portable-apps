from types import SimpleNamespace

from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.graph.nodes import _generator_for_task


def _task(task_type: str) -> GenerationTask:
    fields = {
        "task_id": "task-1",
        "task_type": task_type,
        "title": "真人任务",
        "source_block_ids": ["block-1"],
        "user_intent": "生成视频",
        "prompt": "人物走近镜头",
        "reference_images": [{"asset_id": "image-1", "role": "reference_image", "order": 1}],
        "aspect_ratio": "9:16",
    }
    if task_type == "image_to_video":
        fields.update({"duration": 5, "resolution": "720p"})
    else:
        fields.update({"image_size": "1024x1024"})
    return GenerationTask(**fields)


async def test_real_person_video_uses_portrait_generator() -> None:
    portrait = SimpleNamespace(for_run=lambda run_id: f"portrait:{run_id}")

    class Store:
        async def get_by_run(self, run_id):
            return SimpleNamespace(snapshot=SimpleNamespace(task_type="真人类"))

    services = SimpleNamespace(
        image_generator="chiyun",
        video_generator="seedance",
        portrait_video_generator=portrait,
        production_task_store=Store(),
    )

    provider, generator = await _generator_for_task("run-real", _task("image_to_video"), services)

    assert (provider, generator) == ("volcengine_portrait", "portrait:run-real")


async def test_real_person_image_task_stays_on_chiyun() -> None:
    services = SimpleNamespace(
        image_generator="chiyun",
        video_generator="seedance",
        portrait_video_generator=SimpleNamespace(for_run=lambda _: "portrait"),
        production_task_store=None,
    )

    provider, generator = await _generator_for_task("run-real", _task("image_to_image"), services)

    assert (provider, generator) == ("chiyun", "chiyun")
