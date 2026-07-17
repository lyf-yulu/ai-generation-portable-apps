from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import (
    GraphServices,
    analyze_images,
    audit_plan,
    human_approval,
    ingest_source,
    normalize_document,
    plan_requirements,
    revalidate_approval,
    validate_planned_tasks,
)
from .state import AgentState


def build_graph(services: GraphServices, checkpointer: Any):
    builder = StateGraph(AgentState)
    builder.add_node(
        "ingest_source", partial(ingest_source, services=services)
    )
    builder.add_node(
        "normalize_document", partial(normalize_document, services=services)
    )
    builder.add_node(
        "analyze_images", partial(analyze_images, services=services)
    )
    builder.add_node(
        "plan_requirements", partial(plan_requirements, services=services)
    )
    builder.add_node("audit_plan", partial(audit_plan, services=services))
    builder.add_node(
        "validate_plan", partial(validate_planned_tasks, services=services)
    )
    builder.add_node(
        "human_approval",
        partial(human_approval, services=services),
        destinations=("plan_requirements", "revalidate_approval", END),
    )
    builder.add_node(
        "revalidate_approval",
        partial(revalidate_approval, services=services),
    )

    chain = [
        "ingest_source",
        "normalize_document",
        "analyze_images",
        "plan_requirements",
        "audit_plan",
        "validate_plan",
        "human_approval",
    ]
    builder.add_edge(START, chain[0])
    for source, target in zip(chain, chain[1:]):
        builder.add_edge(source, target)
    builder.add_edge("revalidate_approval", END)
    return builder.compile(checkpointer=checkpointer)
