  1. Docs/quickstart.md (15 min)
  2. Docs/architecture.md (20 min)
  3. main.py - just the imports and WebSocket sections (15 min)
  4. config/config.yml (memory section) (10 min)

  🔧 "I want to work on memory extraction"

  1. Docs/quickstart.md → Docs/memories.md
  2. config/config.yml (memory.extraction section)
  3. main.py lines 1047-1065 (trigger)
  4. main.py lines 1163-1195 (processing)
  5. src/memory/memory_service.py
  6. src/memory_debug.py (for tracking)

  🐛 "I want to debug pipeline issues"

  1. MEMORY_DEBUG_IMPLEMENTATION.md
  2. src/memory_debug.py
  3. src/memory_debug_api.py  
  4. API endpoints: /api/debug/memory/*

  🏗️ "I want to understand the full architecture"

  1. Docs/architecture.md
  2. main.py (full file, focusing on class structures)
  3. src/auth.py (authentication flow)
  4. src/users.py (user management)
  5. All service files (memory_service.py)

  🎯 Key Concepts to Understand

  Data Flow

  Audio → Transcription → Dual Processing
                        └─ Memory Pipeline (end-of-conversation)
