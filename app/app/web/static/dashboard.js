"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const categories = {
    technology: "Технологии",
    business: "Бизнес",
    world: "Мир",
    sports: "Спорт",
  };
  const statuses = {
    queued: "В очереди",
    received: "Приём",
    processing: "В работе",
    published: "Опубликовано",
    skipped: "Пропущено",
    failed: "Ошибка",
    duplicate: "Дубликат",
  };
  const tools = {
    get_source_context: "Прочитан первоисточник",
    find_similar_news: "Проверены похожие новости",
    save_article: "Статья сохранена",
    publish_article: "Финальные проверки выполнены",
  };
  const reasons = {
    not_relevant: "Материал не относится к теме блога — редактор не запущен.",
    auto_publish_disabled:
      "Статья готова. Режим черновика: в Ghost ничего не отправлено.",
    already_processed:
      "Эта новость уже есть в базе. Показан сохранённый результат.",
    classification_retry:
      "Laya временно недоступна. Задача сохранена для повторной классификации.",
    similar_publication_exists: "Похожая новость уже опубликована.",
    missing_identifiable_source_url:
      "Для статьи нужна ссылка на первоисточник.",
    facts_require_verification:
      "Материал требует дополнительной проверки фактов.",
    hourly_publication_limit: "Достигнут часовой лимит публикаций.",
    agent_step_limit: "Агент достиг лимита шагов.",
    generated_article_not_technology:
      "Подготовленная статья не прошла тематическую проверку.",
  };
  let generation = 0,
    polling = null,
    busy = false,
    selectedNewsId = null;
  const exampleTechnology = {
    title: "Группы задач asyncio.TaskGroup в Python",
    name: "Документация Python 3.12",
    url: "https://docs.python.org/3.12/library/asyncio-task.html#task-groups",
    content:
      "Официальная документация Python 3.12 описывает asyncio.TaskGroup. Класс добавлен в Python 3.11. Это асинхронный контекстный менеджер для группы задач. Задачи создаются через TaskGroup.create_task. При выходе из контекста ожидается завершение всех задач. Если задача завершается с исключением, отличным от CancelledError, остальные задачи группы отменяются. После завершения группы ошибки объединяются в ExceptionGroup или BaseExceptionGroup. KeyboardInterrupt и SystemExit после отмены остальных задач пробрасываются отдельно. Источник не приводит бенчмарки.",
  };
  const exampleSports = {
    title: "Тестовый пример: результат футбольного матча",
    name: "Демонстрационный источник",
    url: "https://example.org/sports-demo",
    content:
      "Тестовая спортивная новость для демонстрации тематического фильтра. Футбольная команда выиграла матч со счётом 2:1. Победный гол забили во втором тайме. Тренер прокомментировал игру защитников, а болельщики поддерживали команду до финального свистка. Это вымышленный пример, а не сообщение о реальном матче.",
  };
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const notice = (text = "", error = false) => {
    $("notice").textContent = text;
    $("notice").className = "notice" + (error ? " error" : "");
    $("notice").hidden = !text;
  };
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...options.headers },
    });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Войди в редакцию ещё раз.");
    }
    const data = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(data.detail)
        ? data.detail
            .map((item) => `${item.loc.slice(1).join(".")}: ${item.msg}`)
            .join("; ")
        : data.detail;
      throw new Error(detail || `Ошибка API: ${response.status}`);
    }
    return data;
  }
  function stage(name, state, text) {
    $("stage-" + name).className = "stage " + state;
    $(
      name === "agent"
        ? "agent-text"
        : name === "publish"
          ? "publish-text"
          : name + "-text",
    ).textContent = text;
    $("stage-" + name).querySelector(".stage-mark").textContent =
      state === "done" ? "✓" : "";
  }
  function badge(status, text) {
    $("result-badge").textContent = text || statuses[status] || status;
    $("result-badge").className =
      "badge" +
      (["processing", "published", "queued"].includes(status)
        ? " active"
        : status === "failed"
          ? " error"
          : "");
  }
  function reset() {
    clearTimeout(polling);
    generation += 1;
    selectedNewsId = null;
    $("publish-button").hidden = true;
    $("categories").hidden = true;
    $("categories").replaceChildren();
    $("article-panel").hidden = true;
    $("article-preview").srcdoc = "";
    $("tool-trace").replaceChildren();
    $("outcome-reason").hidden = true;
    $("job-details").hidden = false;
    $("empty-state").hidden = true;
    $("job-progress").textContent = "Подготовка";
    $("job-attempt").textContent = "";
    $("news-id").textContent = "";
    stage("ingress", "", "Проверка и сохранение новости");
    stage("laya", "", "Быстрое решение о маршруте");
    stage("agent", "", "Анализ источника и подготовка статьи");
    stage("publish", "", "Готовая статья или причина отказа");
    notice();
    return generation;
  }
  function renderCategories(probabilities) {
    $("categories").replaceChildren();
    if (!probabilities) return;
    Object.entries(probabilities)
      .sort((a, b) => b[1] - a[1])
      .forEach(([key, value]) => {
        const row = node("div", undefined, "category");
        const track = node("div", undefined, "category-track");
        const bar = node("div", undefined, "category-bar");
        bar.style.width = `${Math.max(0, Math.min(100, value * 100))}%`;
        track.append(bar);
        row.append(
          node("span", categories[key] || key),
          track,
          node("span", `${(value * 100).toFixed(1)}%`, "category-score"),
        );
        $("categories").append(row);
      });
    $("categories").append(
      node("p", "Оценки модели, а не гарантия точности", "category-note"),
    );
    $("categories").hidden = false;
  }
  function renderNews(news) {
    stage("ingress", "done", "Новость сохранена, точного дубликата нет");
    stage(
      "laya",
      news.category ? "done" : "active",
      news.category
        ? `Категория: ${categories[news.category] || news.category}`
        : "Ожидаем классификацию Laya",
    );
    renderCategories(news.category_probabilities);
    $("news-id").textContent = `Новость ${news.id}`;
    badge(news.status);
    if (news.reason) {
      $("outcome-reason").textContent = reasons[news.reason] || news.reason;
      $("outcome-reason").hidden = false;
    }
    if (news.status === "skipped" && news.reason === "not_relevant") {
      stage("agent", "muted", "Редактор не нужен для этой темы");
      stage("publish", "muted", "Материал пропущен тематическим фильтром");
      $("job-progress").textContent = "Gemma не запускалась";
    }
  }
  function renderJob(job) {
    const terminal = ["published", "skipped", "failed"].includes(job.status);
    const publishing = job.publish_requested && !terminal;
    badge(
      job.status,
      publishing
        ? "Публикация"
        : job.publication_status === "dry_run"
          ? "Черновик готов"
          : undefined,
    );
    $("job-progress").textContent =
      `Шаг ${job.current_step} из ${document.body.dataset.maxSteps}`;
    $("job-attempt").textContent = job.attempts
      ? `Попытка ${job.attempts}`
      : "";
    let entries = job.agent_trace || [];
    const requested = entries.findLastIndex(
      (entry) => entry.event === "publish_requested",
    );
    if (requested >= 0) entries = entries.slice(requested + 1);
    const trace = entries.filter((entry) => entry.tool);
    $("tool-trace").replaceChildren();
    trace.forEach((entry) => {
      const item = node(
        "li",
        entry.error
          ? `Инструмент отклонён: ${entry.error}`
          : tools[entry.tool] || entry.tool,
      );
      item.append(node("small", `ШАГ ${entry.step}`));
      $("tool-trace").append(item);
    });
    stage(
      "agent",
      terminal
        ? job.current_step
          ? "done"
          : "muted"
        : job.status === "processing"
          ? "active"
          : "",
      terminal
        ? `Выполнено шагов: ${job.current_step}`
        : job.status === "queued"
          ? "Ожидает свободного worker"
          : "Агент анализирует материал и вызывает инструменты",
    );
    const saved =
      job.publication_status === "published" ||
      (job.publication_status === "dry_run" && !job.publish_requested);
    stage(
      "publish",
      saved ? "done" : publishing ? "active" : terminal ? "muted" : "",
      publishing
        ? "Агент повторно проверяет черновик перед отправкой в Ghost"
        : job.publication_status === "dry_run"
          ? "Проверенный черновик без отправки в Ghost"
          : job.status === "published"
            ? "Статья опубликована в Ghost"
            : job.publication_status === "draft"
              ? "Черновик сохранён, ожидаем финальные проверки"
              : terminal
                ? "Статья не опубликована"
                : "Ожидаем статью и финальные проверки",
    );
    if (job.error) {
      const detail = (job.agent_trace || []).findLast(
        (entry) => entry.error === job.error && entry.detail,
      )?.detail;
      $("outcome-reason").textContent =
        (reasons[job.error] || job.error) +
        (detail ? `\n\nПояснение модели: ${detail}` : "");
      $("outcome-reason").hidden = false;
    }
    return terminal;
  }
  async function showArticle(newsId, ticket) {
    // A not-yet-saved article is normal while the agent is still running.
    const response = await fetch(
      `/v1/news/${encodeURIComponent(newsId)}/article`,
      { credentials: "same-origin", cache: "no-store" },
    );
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (response.status === 404) return;
    if (!response.ok) throw new Error("Не удалось загрузить статью");
    const article = await response.json();
    if (ticket !== generation) return;
    selectedNewsId = newsId;
    $("publish-button").hidden =
      !article.can_publish && !article.publish_pending;
    $("publish-button").disabled = article.publish_pending;
    $("publish-button").textContent = article.publish_pending
      ? "Агент проверяет…"
      : "Опубликовать черновик ↗";
    $("article-title").textContent = article.title;
    $("article-summary").textContent = article.summary;
    $("article-tags").replaceChildren(
      ...article.tags.map((tag) => node("span", tag)),
    );
    $("fact-notes").textContent = article.factuality_notes;
    $("ghost-link").hidden = true;
    if (article.ghost_url) {
      const target = new URL(article.ghost_url);
      if (["https:", "http:"].includes(target.protocol)) {
        $("ghost-link").href = target.href;
        $("ghost-link").hidden = false;
      }
    }
    $("article-preview").srcdoc =
      `<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><style>body{font:16px/1.85 Georgia,serif;color:#304336;padding:15px 0;max-width:850px;margin:0}p{margin:0 0 18px}h2,h3{font-weight:500}a{color:#246c55;pointer-events:none}pre{white-space:pre-wrap}code{font-size:13px}</style></head><body>${article.content_html}</body></html>`;
    $("article-panel").hidden = false;
  }
  async function inspect(newsId, jobId, ticket) {
    if (ticket !== generation) return;
    const news = await api(`/v1/news/${encodeURIComponent(newsId)}`);
    if (ticket !== generation) return;
    renderNews(news);
    let terminal = true;
    if (jobId) {
      const job = await api(`/v1/jobs/${encodeURIComponent(jobId)}`);
      if (ticket !== generation) return;
      terminal = renderJob(job);
      if (job.publication_status) await showArticle(newsId, ticket);
    } else if (
      news.status === "published" ||
      news.reason === "auto_publish_disabled"
    ) {
      await showArticle(newsId, ticket);
    }
    if (ticket !== generation) return;
    if (!terminal)
      polling = setTimeout(
        () =>
          inspect(newsId, jobId, ticket).catch((error) =>
            notice(
              `Обновление остановлено: ${error.message}. Открой материал из истории, чтобы повторить.`,
              true,
            ),
          ),
        2000,
      );
    else await history();
  }
  async function history() {
    const data = await api("/v1/news");
    $("history-list").replaceChildren();
    if (!data.items.length) {
      $("history-list").append(
        node(
          "p",
          "Пока нет материалов. Отправь первую новость выше.",
          "history-empty",
        ),
      );
      return;
    }
    data.items.forEach((news) => {
      const row = node("button", undefined, "history-item");
      row.type = "button";
      const title = node("span");
      title.append(
        node("strong", news.title),
        node(
          "small",
          new Date(
            news.created_at + (news.created_at.endsWith("Z") ? "" : "Z"),
          ).toLocaleString("ru-RU"),
        ),
      );
      row.append(
        title,
        node("span", categories[news.category] || "—", "category-label"),
        node(
          "span",
          news.reason === "auto_publish_disabled"
            ? "Черновик готов"
            : statuses[news.status] || news.status,
          "badge",
        ),
        node("span", "↗", "arrow"),
      );
      row.addEventListener("click", () => {
        const ticket = reset();
        inspect(news.id, news.job_id, ticket).catch((error) =>
          notice(error.message, true),
        );
        $("stage-ingress").scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
      });
      $("history-list").append(row);
    });
  }
  $("news-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    const ticket = reset();
    $("submit-button").disabled = true;
    $("submit-button").textContent = "Классифицируем…";
    stage("ingress", "active", "Проверяем и сохраняем новость");
    stage("laya", "active", "Ожидаем решение Laya");
    badge("processing", "Классификация");
    const payload = {
      source: { type: $("source-type").value, name: $("source-name").value },
      content: $("content").value,
      language: "ru",
    };
    if ($("title").value.trim()) payload.title = $("title").value.trim();
    if ($("source-url").value.trim())
      payload.source.url = $("source-url").value.trim();
    try {
      const result = await api("/v1/news", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await history();
      if (ticket !== generation) return;
      const list = await api("/v1/news");
      const record = list.items.find((item) => item.id === result.id);
      await inspect(result.id, result.job_id || record?.job_id, ticket);
      if (result.status === "duplicate") notice(reasons.already_processed);
      else if (result.reason === "classification_retry")
        notice(reasons.classification_retry);
    } catch (error) {
      if (ticket === generation) {
        badge("failed");
        notice(error.message, true);
      }
    } finally {
      busy = false;
      $("submit-button").disabled = false;
      $("submit-button").textContent = "Отправить агенту →";
    }
  });
  document.querySelectorAll("[data-example]").forEach((button) =>
    button.addEventListener("click", () => {
      const example =
        button.dataset.example === "sports" ? exampleSports : exampleTechnology;
      $("title").value = example.title;
      $("content").value = example.content;
      $("source-name").value = example.name;
      $("source-url").value = example.url;
      $("source-type").value = "manual";
      $("content").dispatchEvent(new Event("input"));
    }),
  );
  $("content").addEventListener("input", () => {
    $("char-count").textContent =
      `${$("content").value.length.toLocaleString("ru-RU")} / 20 000`;
  });
  $("refresh-button").addEventListener("click", () =>
    history().catch((error) => notice(error.message, true)),
  );
  $("publish-button").addEventListener("click", async () => {
    const id = selectedNewsId,
      ticket = generation;
    if (!id || $("publish-button").disabled) return;
    $("publish-button").disabled = true;
    $("publish-button").textContent = "Отправляем агенту…";
    notice();
    try {
      const result = await api(`/v1/news/${encodeURIComponent(id)}/publish`, {
        method: "POST",
      });
      if (ticket !== generation) return;
      clearTimeout(polling);
      $("outcome-reason").hidden = true;
      await inspect(id, result.job_id, ticket);
      await history();
    } catch (error) {
      if (ticket === generation) {
        notice(error.message, true);
        $("publish-button").disabled = false;
        $("publish-button").textContent = "Опубликовать черновик ↗";
      }
    }
  });
  $("logout-button").addEventListener("click", async () => {
    try {
      await api("/auth/logout", { method: "POST" });
      window.location.assign("/login");
    } catch (error) {
      notice(error.message, true);
    }
  });
  history().catch((error) => notice(error.message, true));
})();
