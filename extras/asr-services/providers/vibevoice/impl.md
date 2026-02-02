❯ Can you can form if the integration is working, that is, I want to run the wizard and select the specific provider, and it should configure it to the
  default.yaml and config.yaml. When I do @start.sh from the root of the repository, it should be able to start up these services. Here we also need to change
   how the architecture works.Because earlier we had a system where we had a separate speech-to-text and speaker recognition system. Now with vibevoice ASR,
  we have a combined speech-to-text plus diarization.So we really need to make sureWe are appropriately adding it and not just adding it as a half-baked
  feature.


  I think right now the conversation model has words and segments, and segments have a speaker tag. Even words can have a speaker tag, I think. Please check
  the actual models. This can be a breaking change; that's okay.What we need to do now is make sure that in this Change: we have a cohesive system.

  We can do it in many ways, but let's think about which way makes sense here.
  The end result is we kind of want segments of speech, ideally with timing, but with speaker recognition, for sure. There is vibevoice ASR, which is doing it
   combined. It does not give word time stamps but gives a unified transcript plus diarisation.

  On the other hand, we have ASR plus diarization.
  Here we use parakeet and pyannote respectively.
  let's think about it.
