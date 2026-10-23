from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.api.auth import authorize
from app.db.models import Job, Publication

router = APIRouter(prefix="/v1/jobs", dependencies=[Depends(authorize)])


@router.get("/{job_id}")
async def get_job(job_id: UUID, request: Request):
    async with request.app.state.db.sessions() as session:
        job = await session.get(Job, str(job_id))
        if job is None:
            raise HTTPException(404, "Job not found")
        publication = await session.scalar(select(Publication).where(Publication.job_id == job.id))
        return {
            "id": job.id,
            "news_id": job.news_id,
            "status": job.status,
            "current_step": job.current_step,
            "attempts": job.attempts,
            "error": job.error,
            "ghost_url": publication.ghost_url if publication else None,
            "publication_status": publication.status if publication else None,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
