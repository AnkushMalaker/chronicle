"""Backend-side bridge for the standalone Hermes wake-word service.

The acoustic detector runs in its own container and cannot call the in-process
plugin router. It publishes detections to the ``wakeword:detections`` Redis
stream; :class:`WakeWordDispatcher` consumes that stream and dispatches the
``wake_word.detected`` plugin event so the Hermes plugin handles it identically
to the text keyword trigger.
"""

from advanced_omi_backend.services.wakeword.dispatcher import WakeWordDispatcher

__all__ = ["WakeWordDispatcher"]
