"""Training data for the home-vs-chat intent router.

Binary classification:
  - "home"  -> a request to control the home / lights / devices / moods.
              Routes into the Home Assistant cascade (/conversation -> fuzzy LLM).
  - "other" -> a general question / agent task / chit-chat / out-of-scope.
              Routes to the Hermes agent.

Coverage goal: lots of DIRECT device commands (templated) PLUS curated
INDIRECT / fuzzy home requests ("make it soothing for my eyes") which have no
literal device keyword and are the whole reason we need an embedding model
instead of keyword matching.

Keep training examples UNambiguous. Genuinely borderline phrasings (timers,
"play music", weather) are deliberately NOT in the training set; they live in
AMBIGUOUS_WATCH so we can observe (not score) how the model treats them.
"""

# ---------------------------------------------------------------------------
# DIRECT home commands - generated from templates
# ---------------------------------------------------------------------------

_AREAS = [
    "hall",
    "living room",
    "dining room",
    "study",
    "study room",
    "bedroom",
    "kitchen",
    "office",
    "lounge",
    "the hall",
    "the living room",
    "the bedroom",
]
_AREA_OR_ALL = _AREAS + ["all the", "all", "every", "the"]
_LIGHTWORDS = ["lights", "light", "lamp", "lamps", "tubelight", "bulbs"]
_FANWORDS = ["fan", "fans", "the fan", "ceiling fan"]

_ON_OFF_TEMPLATES = [
    "turn on the {area} {light}",
    "turn off the {area} {light}",
    "switch on the {area} {light}",
    "switch off the {area} {light}",
    "turn the {area} {light} on",
    "turn the {area} {light} off",
    "toggle the {area} {light}",
    "{area} {light} on",
    "{area} {light} off",
    "kill the {area} {light}",
    "shut off the {area} {light}",
    "put on the {area} {light}",
    "put off the {area} {light}",
]

_DIM_TEMPLATES = [
    "dim the {area} {light}",
    "brighten the {area} {light}",
    "dim the {area} {light} a bit",
    "set the {area} {light} to fifty percent",
    "set the {area} {light} to twenty percent",
    "set {area} {light} brightness to thirty",
    "make the {area} {light} brighter",
    "make the {area} {light} dimmer",
    "lower the {area} {light}",
    "increase the {area} {light}",
    "set the {area} {light} to warm white",
    "set the {area} {light} to cool white",
    "make the {area} {light} warmer",
    "make the {area} {light} cooler",
    "change the {area} {light} to blue",
    "change the {area} {light} to red",
]

_FAN_TEMPLATES = [
    "turn on the {area} {fan}",
    "turn off the {area} {fan}",
    "switch off the {area} {fan}",
    "{area} {fan} off",
    "{area} {fan} on",
    "change the {fan} speed",
    "set the {fan} speed to high",
    "set the {fan} speed to low",
    "increase the {fan} speed",
    "turn off all {fan}",
    "speed up the {area} {fan}",
    "slow down the {area} {fan}",
]

_GLOBAL_DIRECT = [
    "turn off all the lights",
    "turn on all the lights",
    "turn off all lights",
    "lights off",
    "lights on",
    "turn everything off",
    "turn off everything",
    "shut everything down",
    "all lights off",
    "switch off all the lights",
    "kill all the lights",
    "turn off the tv",
    "turn on the tv",
    "mute the tv",
    "pause the tv",
    "turn on the ac",
    "turn off the ac",
    "set the ac to twenty four degrees",
    "lock the front door",
    "unlock the door",
    "is the living room light on",
    "are the lights on",
    "which lights are on",
    "turn on the lamp",
    "turn off the lamp",
]


def _expand(templates, light_words, fan_words=None):
    out = set()
    for t in templates:
        if "{fan}" in t:
            for area in _AREA_OR_ALL:
                for fan in fan_words or _FANWORDS:
                    out.add(t.format(area=area, fan=fan).replace("  ", " ").strip())
        else:
            for area in _AREA_OR_ALL:
                for light in light_words:
                    out.add(t.format(area=area, light=light).replace("  ", " ").strip())
    return out


# ---------------------------------------------------------------------------
# INDIRECT / fuzzy home requests - hand curated (NO literal device keyword)
# These are the hard cases. Embedding model must place them near "home".
# ---------------------------------------------------------------------------

_HOME_FUZZY = [
    "make it more soothing for my eyes",
    "make it soothing for my eyes",
    "it's too bright in here",
    "it is too bright",
    "too bright",
    "it's way too bright",
    "the lights are too harsh",
    "this is too harsh on my eyes",
    "make it softer",
    "soften the lighting",
    "make it cozy",
    "make it cozier",
    "make the room cozy",
    "set a cozy mood",
    "make it warm and cozy",
    "warm it up in here",
    "warm up the room",
    "make it warmer in here",
    "make it feel warmer",
    "cool it down a bit",
    "make the lighting cooler",
    "set a relaxing mood",
    "i want to relax",
    "make it relaxing",
    "set the mood for relaxing",
    "set the mood for dinner",
    "set a romantic mood",
    "make it romantic in here",
    "i'm going to watch a movie",
    "movie time",
    "set up movie mode",
    "i want to watch a film",
    "set the lighting for a movie",
    "it's movie night",
    "i'm going to bed",
    "i'm off to bed",
    "getting ready for bed",
    "time for bed",
    "set it for bedtime",
    "bedtime",
    "i'm tired",
    "wind down for the night",
    "time to wind down",
    "i need to focus",
    "set a focus mood",
    "i'm reading",
    "i need some reading light",
    "give me reading light",
    "it's too dark in here",
    "it's too dark",
    "too dim",
    "the room is too dark",
    "brighten things up",
    "brighten it up in here",
    "can you make it brighter",
    "make it less bright",
    "tone it down a bit",
    "evening mode",
    "set evening mode",
    "go to evening mode",
    "night mode",
    "set night mode",
    "activate evening mode",
    "good morning routine",
    "morning mode",
    "set the party mood",
    "party mode",
    "make it festive",
    "chill vibes",
    "set chill vibes",
    "movie mode",
    "focus mode",
    "study mode",
    "reading mode",
    "dinner mode",
    "sleep mode",
    "chill mode",
    "relax mode",
    "lights for a movie",
    "i want movie lighting",
    "time to watch something",
    "make it comfortable in here",
    "the lighting hurts my eyes",
    "my eyes hurt from the light",
    "set the mood",
    "set a nice ambiance",
    "make the ambiance nicer",
    "dim it down",
    "dim it a little",
    "dim things down",
    "darken the room",
    "less light please",
    "more light please",
    "i want warm light",
    "i want soft light",
    "i want dim light",
]

# ---------------------------------------------------------------------------
# OTHER - general agent / chat / questions / out of scope
# ---------------------------------------------------------------------------

_OTHER = [
    # general knowledge / chat
    "what is the meaning of life",
    "what's the meaning of life",
    "tell me a joke",
    "tell me something interesting",
    "what's the capital of france",
    "who won the cricket match last night",
    "what is quantum physics",
    "explain how airplanes fly",
    "what's twenty five times four",
    "translate hello into spanish",
    "how do i make pasta",
    "what's a good recipe for dinner",
    "give me a recipe for biryani",
    "how tall is mount everest",
    "what's the population of india",
    "tell me about the roman empire",
    "what should i cook tonight",
    "how do i tie a tie",
    "what's the difference between a virus and bacteria",
    "why is the sky blue",
    "who is the president of the united states",
    "what year did world war two end",
    "define serendipity",
    "what's a synonym for happy",
    # agent tasks / assistant
    "remind me to call mom",
    "remind me to take my medicine at nine",
    "remind me about the meeting tomorrow",
    "add eggs to my shopping list",
    "add milk to the grocery list",
    "put bread on my shopping list",
    "what's on my calendar today",
    "what's my next meeting",
    "when is my dentist appointment",
    "summarize my day",
    "what did i do yesterday",
    "send a message to john",
    "text sarah that i'll be late",
    "email the team the report",
    "what's the weather today",
    "what's the weather like tomorrow",
    "is it going to rain today",
    "what's the forecast for the weekend",
    "what time is it",
    "what's today's date",
    "how's the traffic to work",
    "read me the news",
    "what's happening in the news",
    "what's the latest on the elections",
    "set a timer for ten minutes",
    "how many calories in an apple",
    "what's my heart rate",
    "how did i sleep last night",
    "what's my schedule for friday",
    "draft an email to my boss",
    "help me write a poem",
    "what's a good book to read",
    "recommend a movie to watch",
    "what's the stock price of apple",
    "convert ten dollars to rupees",
    "how far is the moon",
    "what's the wifi password",
    # out of scope / chit chat / fragments
    "hey there",
    "hello",
    "good morning",
    "good night",
    "thank you",
    "thanks a lot",
    "never mind",
    "forget it",
    "okay cool",
    "sounds good",
    "yeah",
    "no",
    "stop",
    "wait",
    "hmm",
    "let me think",
    "what",
    "huh",
    "are you there",
    "how are you",
    "what can you do",
    "who are you",
    "tell me about yourself",
    # more general knowledge / chat
    "what's the speed of light",
    "how does a car engine work",
    "what's a black hole",
    "explain photosynthesis",
    "what's the largest ocean",
    "who painted the mona lisa",
    "what's the boiling point of water",
    "how many continents are there",
    "what language do they speak in brazil",
    "what's the tallest building in the world",
    "tell me a fun fact",
    "give me a motivational quote",
    "what's the meaning of the word ephemeral",
    "how do i learn to code",
    "what's a good workout routine",
    "explain the theory of relativity",
    "what causes earthquakes",
    "how do vaccines work",
    "what's the difference between coffee and espresso",
    "recommend a good restaurant nearby",
    # more agent tasks
    "remind me to water the plants",
    "remind me to pay the rent",
    "add coffee to the shopping list",
    "put the meeting on my calendar",
    "schedule a call with the team for monday",
    "cancel my three pm meeting",
    "what's my first meeting tomorrow",
    "how long until my next appointment",
    "make a note that the car needs servicing",
    "take a note about the project idea",
    "what tasks do i have today",
    "mark the report as done",
    "send sarah a text",
    "call the dentist",
    "what's the weather in london",
    "will it rain this weekend",
    "how hot is it outside",
    "what's the news headlines",
    "give me a summary of the news",
    "what's the exchange rate for euros",
    "how much is bitcoin right now",
    "set an alarm for six am",
    "what's my step count today",
    "how many emails do i have",
    "read my latest email",
    "what time does the sun set today",
    "how far is it to the airport",
    "book a cab to the office",
    "order a pizza",
    "what's a synonym for difficult",
    "spell the word necessary",
    "what's fifteen percent of two hundred",
    "convert five kilometers to miles",
    # more chit chat / out of scope / fragments
    "good evening",
    "see you later",
    "talk to you soon",
    "that's all",
    "i'm good",
    "perfect thanks",
    "awesome",
    "great",
    "oops",
    "sorry",
    "excuse me",
    "one second",
    "hold on",
    "go ahead",
    "continue",
    "repeat that",
    "say that again",
    "louder please",
    "i didn't catch that",
    "can you hear me",
    "testing one two three",
    "just checking",
    "nothing",
    "forget what i said",
    "you're funny",
    "good bot",
    "i love you",
    "what's up",
    "how's it going",
]

# Phrasings that are genuinely ambiguous - we do NOT train on these,
# we only observe how the trained model routes them.
AMBIGUOUS_WATCH = [
    "play some music",
    "play my focus playlist",
    "set a timer for five minutes",
    "what's the temperature in here",
    "is it cold in here",
    "good night",  # could be "bedtime mode" or just a sign-off
    "i'm cold",  # AC up? or just a statement
    "it's hot",
    "what's the weather",  # weather vs nothing-to-do-with-home
]


def build_dataset(home_direct_cap=240, seed=42):
    """Return (texts, labels) with labels in {'home','other'}.

    The templated direct commands explode into thousands of near-duplicates,
    which swamps the 'other' class and distorts the decision boundary. We
    therefore SAMPLE the direct commands down to `home_direct_cap` and keep
    every curated fuzzy/indirect example (those are the hard, valuable ones),
    giving a roughly balanced set. Held-out REAL_PHRASES are excluded so the
    evaluation in train.py is honest.
    """
    import random

    rng = random.Random(seed)
    held_out = {t for t, _ in REAL_PHRASES}

    direct = set()
    direct |= _expand(_ON_OFF_TEMPLATES, _LIGHTWORDS)
    direct |= _expand(_DIM_TEMPLATES, _LIGHTWORDS)
    direct |= _expand(_FAN_TEMPLATES, _LIGHTWORDS, _FANWORDS)
    direct |= set(_GLOBAL_DIRECT)
    direct -= held_out
    direct = sorted(direct)
    rng.shuffle(direct)
    direct = direct[:home_direct_cap]

    fuzzy = sorted(set(_HOME_FUZZY) - held_out)
    home = sorted(set(direct) | set(fuzzy))
    other = sorted(set(_OTHER) - held_out)

    texts, labels = [], []
    for t in home:
        texts.append(t)
        labels.append("home")
    for t in other:
        texts.append(t)
        labels.append("other")
    return texts, labels


# Real phrases the user actually says - held out for honest evaluation.
REAL_PHRASES = [
    ("turn off hall lights", "home"),
    ("turn off bedroom lights", "home"),
    ("turn off all the lights", "home"),
    ("change fan speed", "home"),
    ("turn off all fans", "home"),
    ("make it more soothing for my eyes", "home"),
    ("turn off the living room lights", "home"),
    ("dim the study lights", "home"),
    ("it's too bright", "home"),
    ("set evening mode", "home"),
    ("i'm going to sleep", "home"),
    ("movie time", "home"),
    ("what is the meaning of life", "other"),
    ("remind me to buy milk", "other"),
    ("what's the weather today", "other"),
    ("tell me a joke", "other"),
    ("what's on my calendar", "other"),
    ("summarize my day", "other"),
]


if __name__ == "__main__":
    texts, labels = build_dataset()
    from collections import Counter

    print("total:", len(texts), Counter(labels))
