"""Common database queries for speaker recognition system."""

from typing import Any, Dict, List, Optional

from sqlalchemy import and_, desc, func
from sqlalchemy.orm import Session

from .models import (
    Annotation,
    EnrollmentSession,
    ExportHistory,
    ProcessingJob,
    Speaker,
    User,
)


class UserQueries:
    """User-related database queries."""

    @staticmethod
    def get_or_create_user(db: Session, username: str) -> User:
        """Get existing user or create new one."""
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(username=username)
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    @staticmethod
    def get_all_users(db: Session) -> List[User]:
        """Get all users with speaker counts."""
        return db.query(User).all()

    @staticmethod
    def get_user_stats(db: Session, user_id: int) -> Dict[str, Any]:
        """Get user statistics."""
        speaker_count = (
            db.query(func.count(Speaker.id)).filter(Speaker.user_id == user_id).scalar()
        )
        annotation_count = (
            db.query(func.count(Annotation.id))
            .filter(Annotation.user_id == user_id)
            .scalar()
        )

        return {"speaker_count": speaker_count, "annotation_count": annotation_count}


class SpeakerQueries:
    """Speaker-related database queries."""

    @staticmethod
    def get_speakers_for_user(db: Session, user_id: int) -> List[Speaker]:
        """Get all speakers for a user."""
        return (
            db.query(Speaker)
            .filter(Speaker.user_id == user_id)
            .order_by(Speaker.name)
            .all()
        )

    @staticmethod
    def get_speaker(db: Session, speaker_id: str) -> Optional[Speaker]:
        """Get speaker by ID."""
        return db.query(Speaker).filter(Speaker.id == speaker_id).first()

    @staticmethod
    def create_speaker(
        db: Session,
        speaker_id: str,
        name: str,
        user_id: int,
        notes: Optional[str] = None,
    ) -> Speaker:
        """Create new speaker."""
        speaker = Speaker(id=speaker_id, name=name, user_id=user_id, notes=notes)
        db.add(speaker)
        db.commit()
        db.refresh(speaker)
        return speaker

    @staticmethod
    def delete_speaker(db: Session, speaker_id: str) -> bool:
        """Delete speaker and all related data."""
        speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
        if speaker:
            db.delete(speaker)
            db.commit()
            return True
        return False

    @staticmethod
    def get_speaker_quality_stats(db: Session, speaker_id: str) -> Dict[str, Any]:
        """Get quality statistics for a speaker."""
        sessions = (
            db.query(EnrollmentSession)
            .filter(EnrollmentSession.speaker_id == speaker_id)
            .all()
        )

        if not sessions:
            return {
                "total_sessions": 0,
                "avg_quality": 0.0,
                "total_duration": 0.0,
                "best_quality": 0.0,
                "latest_session": None,
            }

        total_duration = sum(s.speech_duration_seconds or 0 for s in sessions)
        avg_quality = sum(s.quality_score or 0 for s in sessions) / len(sessions)

        return {
            "total_sessions": len(sessions),
            "avg_quality": avg_quality,
            "total_duration": total_duration,
            "best_quality": max(s.quality_score or 0 for s in sessions),
            "latest_session": max(s.created_at for s in sessions),
        }


class EnrollmentQueries:
    """Enrollment session queries."""

    @staticmethod
    def create_enrollment_session(
        db: Session,
        speaker_id: str,
        audio_file_path: str,
        duration_seconds: float,
        speech_duration_seconds: float,
        quality_score: float,
        snr_db: float,
        enrollment_method: str,
    ) -> EnrollmentSession:
        """Create new enrollment session."""
        session = EnrollmentSession(
            speaker_id=speaker_id,
            audio_file_path=audio_file_path,
            duration_seconds=duration_seconds,
            speech_duration_seconds=speech_duration_seconds,
            quality_score=quality_score,
            snr_db=snr_db,
            enrollment_method=enrollment_method,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get_sessions_for_speaker(
        db: Session, speaker_id: str
    ) -> List[EnrollmentSession]:
        """Get all enrollment sessions for a speaker."""
        return (
            db.query(EnrollmentSession)
            .filter(EnrollmentSession.speaker_id == speaker_id)
            .order_by(desc(EnrollmentSession.created_at))
            .all()
        )


class AnnotationQueries:
    """Annotation-related queries."""

    @staticmethod
    def create_annotation(
        db: Session,
        audio_file_path: str,
        start_time: float,
        end_time: float,
        user_id: int,
        speaker_id: Optional[str] = None,
        speaker_label: Optional[str] = None,
        label: str = "UNCERTAIN",
        confidence: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> Annotation:
        """Create new annotation."""
        annotation = Annotation(
            audio_file_path=audio_file_path,
            start_time=start_time,
            end_time=end_time,
            user_id=user_id,
            speaker_id=speaker_id,
            speaker_label=speaker_label,
            label=label,
            confidence=confidence,
            notes=notes,
        )
        db.add(annotation)
        db.commit()
        db.refresh(annotation)
        return annotation

    @staticmethod
    def get_annotations_for_audio(
        db: Session, audio_file_path: str, user_id: int
    ) -> List[Annotation]:
        """Get all annotations for an audio file."""
        return (
            db.query(Annotation)
            .filter(
                and_(
                    Annotation.audio_file_path == audio_file_path,
                    Annotation.user_id == user_id,
                )
            )
            .order_by(Annotation.start_time)
            .all()
        )

    @staticmethod
    def get_annotations_for_speaker(db: Session, speaker_id: str) -> List[Annotation]:
        """Get all annotations for a speaker."""
        return db.query(Annotation).filter(Annotation.speaker_id == speaker_id).all()

    @staticmethod
    def get_user_annotation_stats(db: Session, user_id: int) -> Dict[str, Any]:
        """Get annotation statistics for a user."""
        total = (
            db.query(func.count(Annotation.id))
            .filter(Annotation.user_id == user_id)
            .scalar()
        )
        correct = (
            db.query(func.count(Annotation.id))
            .filter(and_(Annotation.user_id == user_id, Annotation.label == "CORRECT"))
            .scalar()
        )
        incorrect = (
            db.query(func.count(Annotation.id))
            .filter(
                and_(Annotation.user_id == user_id, Annotation.label == "INCORRECT")
            )
            .scalar()
        )
        uncertain = (
            db.query(func.count(Annotation.id))
            .filter(
                and_(Annotation.user_id == user_id, Annotation.label == "UNCERTAIN")
            )
            .scalar()
        )

        return {
            "total": total,
            "correct": correct,
            "incorrect": incorrect,
            "uncertain": uncertain,
        }


class JobQueries:
    """Processing job queries."""

    @staticmethod
    def create_job(db: Session, job_type: str, input_data: str) -> ProcessingJob:
        """Create new processing job."""
        job = ProcessingJob(job_type=job_type, input_data=input_data)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def update_job_progress(
        db: Session, job_id: int, progress: float, status: str = None
    ) -> bool:
        """Update job progress."""
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        if job:
            job.progress = progress
            if status:
                job.status = status
            db.commit()
            return True
        return False

    @staticmethod
    def complete_job(
        db: Session, job_id: int, output_data: str = None, error_message: str = None
    ) -> bool:
        """Mark job as completed or failed."""
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        if job:
            job.completed_at = func.now()
            if error_message:
                job.status = "failed"
                job.error_message = error_message
            else:
                job.status = "completed"
                job.output_data = output_data
            db.commit()
            return True
        return False


class ExportQueries:
    """Export history queries."""

    @staticmethod
    def create_export_record(
        db: Session,
        user_id: int,
        export_type: str,
        format_type: str,
        file_path: str,
        file_size_bytes: int,
        speaker_ids: str,
    ) -> ExportHistory:
        """Create export history record."""
        export = ExportHistory(
            user_id=user_id,
            export_type=export_type,
            format_type=format_type,
            file_path=file_path,
            file_size_bytes=file_size_bytes,
            speaker_ids=speaker_ids,
        )
        db.add(export)
        db.commit()
        db.refresh(export)
        return export

    @staticmethod
    def get_user_exports(db: Session, user_id: int) -> List[ExportHistory]:
        """Get export history for a user."""
        return (
            db.query(ExportHistory)
            .filter(ExportHistory.user_id == user_id)
            .order_by(desc(ExportHistory.created_at))
            .all()
        )
