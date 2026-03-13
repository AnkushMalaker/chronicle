"""Quality assessment utilities for speaker enrollment."""

from typing import Any, Dict, Tuple

import librosa
import numpy as np

from .audio_processing import calculate_snr


def calculate_duration_score(
    duration_seconds: float, optimal_min: float = 30.0, optimal_max: float = 120.0
) -> float:
    """
    Calculate duration quality score.

    Args:
        duration_seconds: Audio duration in seconds
        optimal_min: Minimum optimal duration
        optimal_max: Maximum optimal duration

    Returns:
        Duration score between 0.0 and 1.0
    """
    if duration_seconds <= 0:
        return 0.0

    if duration_seconds < 5.0:
        # Too short - poor quality
        return duration_seconds / 5.0 * 0.3
    elif duration_seconds < optimal_min:
        # Short but acceptable
        return 0.3 + (duration_seconds - 5.0) / (optimal_min - 5.0) * 0.4
    elif duration_seconds <= optimal_max:
        # Optimal range
        return (
            0.7 + (duration_seconds - optimal_min) / (optimal_max - optimal_min) * 0.3
        )
    else:
        # Longer than optimal - diminishing returns
        excess = duration_seconds - optimal_max
        penalty = min(excess / optimal_max, 0.2)  # Max 0.2 penalty
        return max(1.0 - penalty, 0.8)


def calculate_snr_score(snr_db: float) -> float:
    """
    Calculate SNR quality score.

    Args:
        snr_db: Signal-to-noise ratio in decibels

    Returns:
        SNR score between 0.0 and 1.0
    """
    if snr_db <= 0:
        return 0.0
    elif snr_db < 10:
        # Poor SNR
        return snr_db / 10.0 * 0.4
    elif snr_db < 15:
        # Acceptable SNR
        return 0.4 + (snr_db - 10) / 5.0 * 0.3
    elif snr_db < 20:
        # Good SNR
        return 0.7 + (snr_db - 15) / 5.0 * 0.2
    else:
        # Excellent SNR
        return min(0.9 + (snr_db - 20) / 20.0 * 0.1, 1.0)


def assess_audio_quality(audio: np.ndarray, sr: int) -> Dict[str, Any]:
    """
    Comprehensive audio quality assessment.

    Args:
        audio: Audio signal array
        sr: Sample rate

    Returns:
        Dictionary with quality metrics and scores
    """
    try:
        # Basic metrics
        duration_seconds = len(audio) / sr
        snr_db = calculate_snr(audio)

        # Individual scores
        duration_score = calculate_duration_score(duration_seconds)
        snr_score = calculate_snr_score(snr_db)

        # Overall quality (simplified to duration + SNR only)
        overall_quality = duration_score * 0.6 + snr_score * 0.4

        # Quality level
        if overall_quality >= 0.8:
            quality_level = "Excellent"
            quality_color = "green"
        elif overall_quality >= 0.6:
            quality_level = "Good"
            quality_color = "blue"
        elif overall_quality >= 0.4:
            quality_level = "Acceptable"
            quality_color = "orange"
        else:
            quality_level = "Poor"
            quality_color = "red"

        return {
            # Raw metrics
            "duration_seconds": duration_seconds,
            "snr_db": snr_db,
            # Individual scores
            "duration_score": duration_score,
            "snr_score": snr_score,
            # Overall assessment
            "overall_quality": overall_quality,
            "quality_level": quality_level,
            "quality_color": quality_color,
            # Recommendations
            "recommendations": generate_quality_recommendations(
                duration_seconds, snr_db, overall_quality
            ),
        }

    except Exception as e:
        return {
            "duration_seconds": 0.0,
            "snr_db": 0.0,
            "duration_score": 0.0,
            "snr_score": 0.0,
            "overall_quality": 0.0,
            "quality_level": "Error",
            "quality_color": "red",
            "recommendations": [f"Error assessing quality: {str(e)}"],
        }


def generate_quality_recommendations(
    duration_seconds: float, snr_db: float, overall_quality: float
) -> list:
    """
    Generate recommendations for improving audio quality.

    Args:
        duration_seconds: Audio duration
        snr_db: Signal-to-noise ratio
        overall_quality: Overall quality score

    Returns:
        List of recommendation strings
    """
    recommendations = []

    # Duration recommendations
    if duration_seconds < 10:
        recommendations.append(
            "🎤 Record more audio - at least 30 seconds is recommended"
        )
    elif duration_seconds < 30:
        recommendations.append("⏱️ Consider recording more for better speaker model")
    elif duration_seconds > 300:
        recommendations.append(
            "✂️ Audio is very long - consider using shorter, high-quality segments"
        )

    # SNR recommendations
    if snr_db < 10:
        recommendations.append(
            "🔇 Reduce background noise - find a quieter environment"
        )
    elif snr_db < 15:
        recommendations.append("🎚️ Improve audio quality - move closer to microphone")

    # Overall recommendations
    if overall_quality < 0.4:
        recommendations.append("⚠️ Audio quality is poor - consider re-recording")
    elif overall_quality < 0.6:
        recommendations.append("📈 Audio quality is acceptable but could be improved")
    elif overall_quality >= 0.8:
        recommendations.append("✅ Excellent audio quality for speaker enrollment!")

    # General tips if quality is not excellent
    if overall_quality < 0.8:
        recommendations.extend(
            [
                "💡 Tip: Use a consistent distance from microphone",
                "🏠 Tip: Record in a quiet room with minimal echo",
                "🎯 Tip: Ensure good microphone positioning",
            ]
        )

    return recommendations


def is_quality_sufficient_for_enrollment(
    quality_score: float, min_threshold: float = 0.4
) -> bool:
    """
    Check if audio quality is sufficient for speaker enrollment.

    Args:
        quality_score: Overall quality score (0.0 to 1.0)
        min_threshold: Minimum acceptable quality threshold

    Returns:
        True if quality is sufficient for enrollment
    """
    return quality_score >= min_threshold


def get_quality_feedback_message(quality_assessment: Dict[str, Any]) -> str:
    """
    Generate a user-friendly quality feedback message.

    Args:
        quality_assessment: Quality assessment dictionary

    Returns:
        Formatted feedback message
    """
    quality = quality_assessment["overall_quality"]
    level = quality_assessment["quality_level"]

    if quality >= 0.8:
        return f"🎉 {level} quality ({quality:.1%})! Perfect for speaker enrollment."
    elif quality >= 0.6:
        return f"👍 {level} quality ({quality:.1%}). Suitable for enrollment."
    elif quality >= 0.4:
        return f"⚠️ {level} quality ({quality:.1%}). May work but consider improving."
    else:
        return f"❌ {level} quality ({quality:.1%}). Please improve before enrollment."
