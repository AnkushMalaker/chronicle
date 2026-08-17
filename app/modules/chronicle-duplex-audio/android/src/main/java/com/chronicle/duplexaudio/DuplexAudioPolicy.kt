package com.chronicle.duplexaudio

internal enum class DuplexMode {
  FULL,
  ISOLATED,
  HALF,
}

internal object DuplexAudioPolicy {
  fun mode(isolatedOutput: Boolean, speakerphone: Boolean, aecEnabled: Boolean): DuplexMode = when {
    isolatedOutput -> DuplexMode.ISOLATED
    speakerphone && aecEnabled -> DuplexMode.FULL
    else -> DuplexMode.HALF
  }

  fun shouldCancel(
    current: EpochResponse?,
    responseId: String,
    cancellationGeneration: Int,
  ): Boolean = current != null &&
    (responseId == "*" || responseId == current.id) &&
    cancellationGeneration >= current.generation
}

internal data class EpochResponse(
  val id: String,
  val generation: Int,
  val captureEpoch: Int,
)

internal class ResponseEpochGate {
  private var captureEpoch = -1
  private var response: EpochResponse? = null

  fun start(epoch: Int) {
    captureEpoch = epoch
    response = null
  }

  fun schedule(next: EpochResponse) {
    require(next.captureEpoch == captureEpoch) { "stale capture epoch" }
    check(response == null) { "one response is already scheduled" }
    response = next
  }

  fun cancel(): EpochResponse? = response.also { response = null }

  fun systemChanged(): EpochResponse? = cancel()
}
