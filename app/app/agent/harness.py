import asyncio
import json

from pydantic import ValidationError
from sqlalchemy import text

from app.agent.prompts import build_system_prompt
from app.agent.registry import TOOLS, tool_definitions
from app.agent.state import RejectArticle
from app.agent.tools.sources import require_lease
from app.schemas.agent import AgentConclusion


async def checkpoint(ctx, step: int, entry: dict):
    ctx.state.trace.append(entry)
    async with ctx.db.sessions() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        job = await require_lease(session, ctx.state)
        job.current_step, job.agent_trace = step, list(ctx.state.trace)
        await session.commit()


async def run_agent(ctx, llm):
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(ctx.settings.demo_mode, ctx.state.publish_requested),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "Проверь и опубликуй сохранённый черновик"
                    if ctx.state.publish_requested
                    else "Подготовь технологическую публикацию",
                    "news_id": ctx.state.news_id,
                    "auto_publish": ctx.settings.auto_publish or ctx.state.publish_requested,
                },
                ensure_ascii=False,
            ),
        },
    ]
    definitions = tool_definitions(ctx.state.publish_requested)
    allowed_names = {tool["function"]["name"] for tool in definitions}
    async with asyncio.timeout(ctx.settings.agent_timeout_seconds):
        for step in range(1, ctx.settings.agent_max_steps + 1):
            message = await llm.complete(messages, definitions)
            calls = message.get("tool_calls", [])
            # Only known wire fields go back to llama.cpp.
            assistant = {"role": "assistant", "content": message.get("content")}
            if calls:
                assistant["tool_calls"] = calls
            messages.append(assistant)
            await checkpoint(
                ctx,
                step,
                {
                    "step": step,
                    "event": "model_response",
                    "tool_names": [c.get("function", {}).get("name") for c in calls],
                },
            )
            if not calls:
                try:
                    conclusion = AgentConclusion.model_validate_json(message.get("content") or "")
                except ValidationError:
                    await checkpoint(ctx, step, {"step": step, "event": "protocol_correction"})
                    messages.append(
                        {
                            "role": "system",
                            "content": "Ответ не соответствует протоколу. Продолжи разрешёнными "
                            "tool_calls либо верни только JSON с outcome и reason. "
                            "Текст статьи вне save_article не сохраняется и не публикуется.",
                        }
                    )
                    continue
                if conclusion.outcome == "completed":
                    await checkpoint(ctx, step, {"step": step, "event": "premature_completion"})
                    messages.append(
                        {
                            "role": "system",
                            "content": "Завершение ещё не подтверждено приложением. Вызови "
                            "publish_article после save_article. Это обязательно и при "
                            "AUTO_PUBLISH=false: инструмент проверит статью и сохранит "
                            "dry_run без публикации. Либо верни outcome=skipped с причиной.",
                        }
                    )
                    continue
                return conclusion
            if len(calls) > 4:
                raise RejectArticle("too_many_tool_calls")
            for call in calls:
                name = call.get("function", {}).get("name")
                if name not in allowed_names:
                    result = {"error": "unknown_tool"}
                else:
                    schema, function, _ = TOOLS[name]
                    try:
                        args = schema.model_validate_json(call["function"].get("arguments", "{}"))
                    except ValidationError as error:
                        result = {
                            "error": "invalid_tool_arguments",
                            "validation_errors": [
                                {
                                    "field": ".".join(map(str, item["loc"])),
                                    "message": item["msg"],
                                }
                                for item in error.errors(include_input=False, include_url=False)[
                                    :10
                                ]
                            ],
                        }
                    except TypeError:
                        result = {"error": "invalid_tool_arguments"}
                    else:
                        try:
                            result = await function(ctx, args)
                        except RejectArticle as error:
                            await checkpoint(
                                ctx,
                                step,
                                {
                                    "step": step,
                                    "tool": name,
                                    "error": str(error),
                                    "detail": error.detail,
                                },
                            )
                            raise
                await checkpoint(
                    ctx,
                    step,
                    {
                        "step": step,
                        "tool": name,
                        "result_status": result.get("status"),
                        "error": result.get("error"),
                        **(
                            {"validation_errors": result["validation_errors"]}
                            if "validation_errors" in result
                            else {}
                        ),
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                # Once publication or dry-run succeeds, no further model call can
                # turn a successful operation into a failed job.
                if name == "publish_article" and result.get("status") in {"published", "dry_run"}:
                    return AgentConclusion(outcome="completed", reason=result["status"])
        raise RejectArticle("agent_step_limit")
