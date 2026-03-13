"""Visualization utilities for audio and speaker data."""

from typing import Any, Dict, List, Optional, Tuple

import librosa
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE


def create_waveform_plot(
    audio: np.ndarray,
    sr: int,
    title: str = "Audio Waveform",
    height: int = 400,
    segments: List[Tuple[float, float]] = None,
    segment_colors: List[str] = None,
) -> go.Figure:
    """
    Create an interactive waveform plot using Plotly with optional segment overlays.

    Args:
        audio: Audio signal array
        sr: Sample rate
        title: Plot title
        height: Plot height in pixels
        segments: Optional list of (start_time, end_time) tuples for segment overlays
        segment_colors: Optional list of colors for segment overlays

    Returns:
        Plotly figure object
    """
    # Create time axis
    time = np.linspace(0, len(audio) / sr, len(audio))

    # Downsample for display if audio is too long
    max_points = 10000
    if len(audio) > max_points:
        step = len(audio) // max_points
        audio = audio[::step]
        time = time[::step]

    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=time,
            y=audio,
            mode="lines",
            name="Waveform",
            line=dict(color="#1f77b4", width=1),
            hovertemplate="Time: %{x:.2f}s<br>Amplitude: %{y:.3f}<extra></extra>",
        )
    )

    # Add segment overlays if provided
    if segments:
        if segment_colors is None:
            segment_colors = px.colors.qualitative.Set3[: len(segments)]

        for i, (start, end) in enumerate(segments):
            color = segment_colors[i % len(segment_colors)]
            # Add semi-transparent rectangle overlay
            fig.add_shape(
                type="rect",
                x0=start,
                x1=end,
                y0=min(audio),
                y1=max(audio),
                fillcolor=color,
                opacity=0.3,
                layer="below",
                line_width=0,
            )
            # Add invisible scatter plot for click detection
            fig.add_trace(
                go.Scatter(
                    x=[start, end],
                    y=[0, 0],
                    mode="markers",
                    marker=dict(size=10, opacity=0),
                    name=f"Segment {i+1}",
                    customdata=[[i, start, end], [i, start, end]],
                    hovertemplate=f"Segment {i+1}<br>Start: {start:.2f}s<br>End: {end:.2f}s<br><i>Click to play</i><extra></extra>",
                    showlegend=False,
                )
            )

    fig.update_layout(
        title=title,
        xaxis_title="Time (seconds)",
        yaxis_title="Amplitude",
        height=height,
        hovermode="x unified",
        showlegend=False,
    )

    # Enable selection for segment picking
    fig.update_layout(
        dragmode="select",
        selectdirection="h",  # 'h' for horizontal, 'v' for vertical, 'd' for diagonal, 'any' for any direction
    )

    return fig


def create_spectrogram_plot(
    audio: np.ndarray,
    sr: int,
    title: str = "Spectrogram",
    height: int = 400,
    n_fft: int = 2048,
    hop_length: int = 512,
) -> go.Figure:
    """
    Create an interactive spectrogram plot.

    Args:
        audio: Audio signal array
        sr: Sample rate
        title: Plot title
        height: Plot height in pixels
        n_fft: FFT window size
        hop_length: Hop length for STFT

    Returns:
        Plotly figure object
    """
    # Compute spectrogram
    D = librosa.stft(audio, n_fft=n_fft, hop_length=hop_length)
    magnitude_db = librosa.amplitude_to_db(np.abs(D), ref=np.max)

    # Create frequency and time axes
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    times = librosa.frames_to_time(
        np.arange(magnitude_db.shape[1]), sr=sr, hop_length=hop_length
    )

    fig = go.Figure(
        data=go.Heatmap(
            z=magnitude_db,
            x=times,
            y=freqs,
            colorscale="Viridis",
            hovertemplate="Time: %{x:.2f}s<br>Frequency: %{y:.0f}Hz<br>Magnitude: %{z:.1f}dB<extra></extra>",
            name="Spectrogram",
        )
    )

    fig.update_layout(
        title=title,
        xaxis_title="Time (seconds)",
        yaxis_title="Frequency (Hz)",
        height=height,
    )

    return fig


def create_segment_timeline(
    segments: List[Tuple[float, float]],
    labels: List[str] = None,
    colors: List[str] = None,
    total_duration: float = None,
    height: int = 200,
    clickable: bool = True,
) -> go.Figure:
    """
    Create a timeline visualization of audio segments with optional click interaction.

    Args:
        segments: List of (start_time, end_time) tuples
        labels: List of segment labels
        colors: List of colors for segments
        total_duration: Total audio duration
        height: Plot height in pixels
        clickable: Whether segments should be clickable

    Returns:
        Plotly figure object
    """
    if not segments:
        fig = go.Figure()
        fig.update_layout(title="No segments to display", height=height)
        return fig

    if labels is None:
        labels = [f"Segment {i+1}" for i in range(len(segments))]

    if colors is None:
        colors = px.colors.qualitative.Set3[: len(segments)]
        if len(segments) > len(colors):
            colors = colors * (len(segments) // len(colors) + 1)

    fig = go.Figure()

    for i, (start, end) in enumerate(segments):
        # Add custom data for click detection
        custom_data = [i, start, end] if clickable else None
        hover_text = f"{labels[i]}<br>Start: {start:.2f}s<br>End: {end:.2f}s<br>Duration: {end-start:.2f}s"
        if clickable:
            hover_text += "<br><i>Click to play audio</i>"

        fig.add_trace(
            go.Scatter(
                x=[start, end, end, start, start],
                y=[i, i, i + 0.8, i + 0.8, i],
                fill="toself",
                fillcolor=colors[i],
                line=dict(color=colors[i], width=2),
                name=labels[i],
                customdata=[custom_data] * 5 if clickable else None,
                hovertemplate=hover_text + "<extra></extra>",
                mode="lines+markers" if clickable else "lines",
            )
        )

    # Set axis properties
    if total_duration:
        x_range = [0, total_duration]
    else:
        x_range = [0, max(end for _, end in segments)]

    fig.update_layout(
        title="Audio Segments Timeline",
        xaxis_title="Time (seconds)",
        yaxis_title="Segments",
        height=height,
        xaxis=dict(range=x_range),
        yaxis=dict(
            range=[-0.5, len(segments) - 0.5],
            tickmode="array",
            tickvals=list(range(len(segments))),
            ticktext=labels,
        ),
        showlegend=False,
    )

    return fig


def create_speaker_similarity_matrix(
    similarity_matrix: np.ndarray,
    speaker_names: List[str],
    title: str = "Speaker Similarity Matrix",
) -> go.Figure:
    """
    Create a heatmap of speaker similarity scores.

    Args:
        similarity_matrix: Square matrix of similarity scores
        speaker_names: List of speaker names
        title: Plot title

    Returns:
        Plotly figure object
    """
    fig = go.Figure(
        data=go.Heatmap(
            z=similarity_matrix,
            x=speaker_names,
            y=speaker_names,
            colorscale="RdYlBu_r",
            zmid=0.5,
            hovertemplate="%{y} vs %{x}<br>Similarity: %{z:.3f}<extra></extra>",
            colorbar=dict(title="Similarity Score"),
        )
    )

    fig.update_layout(title=title, xaxis_title="Speaker", yaxis_title="Speaker")

    return fig


def create_embedding_visualization(
    embeddings: np.ndarray,
    speaker_names: List[str],
    method: str = "tsne",
    title: str = "Speaker Embeddings Visualization",
) -> go.Figure:
    """
    Create 2D visualization of speaker embeddings.

    Args:
        embeddings: Array of speaker embeddings (n_speakers, embedding_dim)
        speaker_names: List of speaker names
        method: Dimensionality reduction method ('tsne' or 'pca')
        title: Plot title

    Returns:
        Plotly figure object
    """
    if method == "tsne":
        reducer = TSNE(
            n_components=2, random_state=42, perplexity=min(30, len(embeddings) - 1)
        )
    else:  # PCA
        reducer = PCA(n_components=2, random_state=42)

    # Reduce dimensionality
    embeddings_2d = reducer.fit_transform(embeddings)

    # Create DataFrame for easier plotting
    df = pd.DataFrame(
        {"x": embeddings_2d[:, 0], "y": embeddings_2d[:, 1], "speaker": speaker_names}
    )

    fig = px.scatter(
        df, x="x", y="y", color="speaker", title=title, hover_data=["speaker"]
    )

    fig.update_layout(
        xaxis_title=f"{method.upper()} Component 1",
        yaxis_title=f"{method.upper()} Component 2",
    )

    return fig


def create_quality_metrics_plot(
    metrics: Dict[str, List[float]],
    speaker_names: List[str],
    title: str = "Speaker Quality Metrics",
) -> go.Figure:
    """
    Create a plot showing quality metrics for speakers.

    Args:
        metrics: Dictionary with metric names as keys and lists of values
        speaker_names: List of speaker names
        title: Plot title

    Returns:
        Plotly figure object
    """
    fig = go.Figure()

    colors = px.colors.qualitative.Set1

    for i, (metric_name, values) in enumerate(metrics.items()):
        fig.add_trace(
            go.Bar(
                name=metric_name,
                x=speaker_names,
                y=values,
                marker_color=colors[i % len(colors)],
                hovertemplate=f"{metric_name}: %{{y:.2f}}<extra></extra>",
            )
        )

    fig.update_layout(
        title=title, xaxis_title="Speaker", yaxis_title="Score", barmode="group"
    )

    return fig


def create_audio_stats_plot(
    durations: List[float],
    snr_values: List[float],
    quality_scores: List[float],
    speaker_names: List[str],
    title: str = "Audio Quality Statistics",
) -> go.Figure:
    """
    Create subplots showing various audio quality statistics.

    Args:
        durations: List of audio durations in seconds
        snr_values: List of SNR values in dB
        quality_scores: List of overall quality scores (0-1)
        speaker_names: List of speaker names
        title: Plot title

    Returns:
        Plotly figure object with subplots
    """
    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=(
            "Duration (seconds)",
            "SNR (dB)",
            "Quality Score",
            "Quality Distribution",
        ),
        specs=[
            [{"type": "bar"}, {"type": "bar"}],
            [{"type": "bar"}, {"type": "histogram"}],
        ],
    )

    # Duration plot
    fig.add_trace(
        go.Bar(x=speaker_names, y=durations, name="Duration", marker_color="lightblue"),
        row=1,
        col=1,
    )

    # SNR plot
    fig.add_trace(
        go.Bar(x=speaker_names, y=snr_values, name="SNR", marker_color="lightgreen"),
        row=1,
        col=2,
    )

    # Quality score plot
    fig.add_trace(
        go.Bar(
            x=speaker_names,
            y=quality_scores,
            name="Quality Score",
            marker_color="lightcoral",
        ),
        row=2,
        col=1,
    )

    # Quality distribution histogram
    fig.add_trace(
        go.Histogram(
            x=quality_scores, name="Quality Distribution", marker_color="gold"
        ),
        row=2,
        col=2,
    )

    fig.update_layout(title_text=title, showlegend=False, height=600)

    return fig
