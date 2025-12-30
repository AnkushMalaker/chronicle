# Chronicle Backend Documentation Guide

## 📖 **New Developer Reading Order**

Welcome to chronicle! This guide provides the optimal reading sequence to understand the complete voice → transcript → memories system.

---

## 🎯 **Start Here: System Overview**

### 1. **[Overview & Quick Start](./quickstart.md)** ⭐ *START HERE*
**Read first** - Complete system overview and setup guide
- What the system does (voice → memories)
- Key features and capabilities
- Basic setup and configuration
- **Code References**: `src/advanced_omi_backend/main.py`, `config/config.yml`, `docker-compose.yml`

### 2. **[System Architecture](./architecture.md)** 
**Read second** - Complete technical architecture with diagrams
- Component relationships and data flow
- Authentication and security architecture
- Deployment structure and containers
- **Code References**: `src/advanced_omi_backend/main.py`, `src/advanced_omi_backend/auth.py`, `src/advanced_omi_backend/users.py`

---

## 🔧 **Core Components Deep Dive**

### 3. **[Memory System](./memories.md)**
**Memory extraction and semantic search**

### 3a. **[Memory Configuration Guide](./memory-configuration-guide.md)** 🎯 *NEW USER GUIDE*
**Easy guide for configuring memory extraction**
- 3-step setup for memory extraction
- Understanding memory types (general, facts, categories)
- Customization examples and troubleshooting
- **Perfect for**: New users wanting to customize memory behavior
- How conversations become memories
- Mem0 integration and vector storage
- Configuration and customization options
- **Code References**: 
  - `src/advanced_omi_backend/memory/memory_service.py` (main processing)
  - `src/advanced_omi_backend/transcript_coordinator.py` (event coordination)
  - `src/advanced_omi_backend/conversation_repository.py` (data access)
  - `src/advanced_omi_backend/conversation_manager.py` (lifecycle management)

### 4. **[Authentication System](./auth.md)**
**User management and security**
- Dual authentication (email + user_id)
- JWT tokens and OAuth integration
- User-centric data architecture
- **Code References**:
  - `src/advanced_omi_backend/auth.py` (authentication logic)
  - `src/advanced_omi_backend/users.py` (user management)
  - `src/advanced_omi_backend/routers/api_router.py` (API router setup)

---

## 🐛 **Advanced Topics**

### 5. **Memory Debug System** → `../MEMORY_DEBUG_IMPLEMENTATION.md`
**Pipeline tracking and debugging**
- How to track transcript → memory conversion
- Debug database schema and API endpoints
- Performance monitoring and troubleshooting
- **Code References**:
  - `src/advanced_omi_backend/routers/modules/system_routes.py` (debug endpoints)

---

## 🔍 **Configuration & Customization**

### 6. **Configuration File** → `../config/config.yml`
**Central configuration for all extraction**
- Memory extraction settings and prompts
- Quality control and debug settings
- **Code References**:
  - `src/advanced_omi_backend/model_registry.py` (config loading)
  - `src/advanced_omi_backend/memory/memory_service.py` (config usage)

---

## 🚀 **Quick Reference by Use Case**

### **"I want to understand the system quickly"** (30 min)
1. [quickstart.md](./quickstart.md) - System overview
2. [architecture.md](./architecture.md) - Technical architecture  
3. `src/advanced_omi_backend/main.py` - Core imports and setup
4. `config/config.yml` - Configuration overview

### **"I want to work on memory extraction"**
1. [memories.md](./memories.md) - Memory system details
2. `../config/config.yml` - Models and memory configuration
3. `src/advanced_omi_backend/memory/memory_service.py` - Implementation
4. `src/advanced_omi_backend/controllers/memory_controller.py` - Processing triggers

### **"I want to debug pipeline issues"**
1. `../MEMORY_DEBUG_IMPLEMENTATION.md` - Debug system overview
3. API: `GET /api/debug/memory/stats` - Live debugging
4. `src/advanced_omi_backend/routers/modules/system_routes.py` - Debug endpoints

### **"I want to understand authentication"**
1. [auth.md](./auth.md) - Authentication system
2. `src/advanced_omi_backend/auth.py` - Authentication implementation
3. `src/advanced_omi_backend/users.py` - User management
4. `src/advanced_omi_backend/routers/api_router.py` - Auth router setup

---

## 📂 **File Organization Reference**

```
backends/advanced-backend/
├── Docs/                           # 📖 Documentation
│   ├── README.md                   # 👈 This file (start here)
│   ├── quickstart.md              # System overview & setup
│   ├── architecture.md            # Technical architecture
│   ├── memories.md                # Memory system details
│   └── auth.md                    # Authentication system
│
├── src/advanced_omi_backend/      # 🔧 Source Code
│   ├── main.py                    # Core application (WebSocket)
│   ├── auth.py                    # Authentication system
│   ├── users.py                   # User management
│   ├── routers/                   # API route definitions
│   │   ├── api_router.py          # Main API router
│   │   └── modules/               # Modular route organization
│   ├── controllers/               # Business logic controllers
│   ├── memory/
│   │   └── memory_service.py      # Memory system (Mem0)
│   └── model_registry.py          # Configuration loading
│
├── config/config.yml                     # 📋 Central configuration
├── MEMORY_DEBUG_IMPLEMENTATION.md # Debug system details
```

---

## 🎯 **Key Code Entry Points**

### **Audio Processing Pipeline**
- **Entry**: WebSocket endpoints in `src/advanced_omi_backend/main.py`
- **Transcription**: Audio processing pipeline in `src/advanced_omi_backend/processors.py`
- **Memory Trigger**: Memory processing in `src/advanced_omi_backend/controllers/memory_controller.py`

### **Data Storage**
- **Memories**: `src/advanced_omi_backend/memory/memory_service.py` → Mem0 → Qdrant

### **Configuration**
- **Loading**: `src/advanced_omi_backend/model_registry.py`
- **File**: `config/config.yml`
- **Usage**: `src/advanced_omi_backend/memory/memory_service.py`

### **Authentication**
- **Setup**: `src/advanced_omi_backend/auth.py`
- **Users**: `src/advanced_omi_backend/users.py`  
- **Integration**: `src/advanced_omi_backend/routers/api_router.py`

---

## 💡 **Reading Tips**

1. **Follow the references**: Each doc links to specific code files and line numbers
2. **Use the debug API**: `GET /api/debug/memory/stats` shows live system status
3. **Check configuration first**: Many behaviors are controlled by `config/config.yml`
4. **Understand the memory pipeline**: Memories (end-of-conversation)
5. **Test with curl**: All API endpoints have curl examples in the docs

---

## 🎯 **After Reading This Guide**

### **Next Steps for New Developers**

1. **Set up the system**: Follow [quickstart.md](./quickstart.md) to get everything running
2. **Test the API**: Use the curl examples in the documentation to test endpoints
3. **Explore the debug system**: Check `GET /api/debug/memory/stats` to see live data
4. **Modify configuration**: Edit `config/config.yml` (memory section) to see how it affects extraction
5. **Read the code**: Start with `src/advanced_omi_backend/main.py` and follow the references in each doc

### **Contributing Guidelines**

- **Add code references**: When updating docs, include file paths and line numbers
- **Test your changes**: Use the debug API to verify your modifications work
- **Update configuration**: Add new settings to `config/config.yml` when needed
- **Follow the architecture**: Keep memories in their respective services

### **Getting Help**

- **Debug API**: `GET /api/debug/memory/*` endpoints show real-time system status
- **Configuration**: Check `config/config.yml` for behavior controls
- **Logs**: Check Docker logs with `docker compose logs chronicle-backend`
- **Documentation**: Each doc file links to relevant code sections

---

This documentation structure ensures you understand both the **big picture** and **implementation details** in a logical progression!
