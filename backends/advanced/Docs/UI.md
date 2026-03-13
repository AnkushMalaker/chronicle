# React Web Dashboard Documentation

## Overview

The Chronicle web dashboard provides a comprehensive interface for managing conversations, memories, users, and system debugging. Built with modern React and TypeScript, it offers real-time access to audio processing pipelines, advanced memory search, and administrative functions.

> **Note**: This documentation covers the modern React interface located in `./webui/`. The legacy Streamlit interface has been moved to `src/_webui_original/` for reference.

## Access & Authentication

### Dashboard URL
- **HTTP**: `http://localhost:5173` (development) or `http://localhost:3000` (production)
- **HTTPS**: `https://localhost/` (automatic via setup wizard - see [Docs/ssl-certificates.md](../../../Docs/ssl-certificates.md))
- **Live Recording**: Available at `/live-record` page for real-time audio streaming
- **Network Access**: Configure `BACKEND_PUBLIC_URL` for remote device access via Tailscale/LAN

### Authentication Methods
1. **Email/Password Login**: Standard authentication via backend API
2. **JWT Token**: Direct token authentication (for programmatic access)
3. **Google OAuth**: Social login integration (if configured)

### User Roles
- **Regular Users**: Access to own conversations and memories
- **Admin Users**: Full system access including debug tools and user management

## Dashboard Sections

### 1. Conversations Tab
**Purpose**: View and manage audio conversations and transcripts

**Features**:
- Real-time conversation listing with metadata
- Audio playback and transcript viewing
- Conversation status tracking (open/closed)
- Speaker identification and timing information
- Audio file upload for processing existing recordings

**Admin Features**:
- View all users' conversations
- Advanced filtering and search capabilities

### 2. Memories Tab
**Purpose**: Browse and search extracted conversation memories with advanced filtering

**Core Search Features**:
- **Text Search**: Traditional keyword-based memory filtering
- **Semantic Search Button**: AI-powered contextual memory search with relevance scoring
- **Dual-layer Filtering**: Combine semantic results with text search for precise filtering
- **Relevance Threshold Slider**: 0-100% threshold to filter semantic results by confidence
- **Active Filter Indicators**: Visual feedback showing active semantic filters with clear buttons
- **Memory Count Display**: Shows "X of Y memories" with total count from native providers

**Advanced Features**:
- **Live Filtering**: Real-time results as threshold slider moves
- **Reset Functionality**: Clear all filters and return to full memory list
- Memory categorization and tagging
- Temporal filtering and sorting
- Memory source tracking (which conversation)
- Export capabilities

**UI Improvements**:
- **Search Input Enhancement**: Semantic search button integrated into search field
- **Visual Feedback**: Loading states, relevance scores, and filter status indicators
- **Responsive Design**: Works across desktop and mobile devices

**Admin Features**:
- **Admin Debug Section**: Load and view all user memories for debugging
- **System-wide Memory View**: Access all memories across users
- **Memory Statistics**: Processing success rates and performance metrics

### 3. User Management Tab
**Purpose**: Administrative user and client management

**Features**:
- **Create New Users**: Email-based user registration
- **User Listing**: View all registered users with details
- **User Deletion**: Remove users and optionally clean up their data
- **Client Management**: View active audio clients and connections

**Admin Only**: This entire tab requires superuser privileges

### 4. Conversation Management Tab
**Purpose**: Real-time conversation and client control

**Features**:
- **Active Clients**: View currently connected audio clients
- **Conversation Control**: Manually close open conversations
- **Connection Monitoring**: Real-time client status and metadata
- **WebSocket Information**: Authentication tokens and connection details

### 5. 🔧 System State Tab (Admin Only)
**Purpose**: Real-time system monitoring, debugging, and failure recovery status

**Important**: This tab uses **lazy loading** - click the buttons to load specific data sections. This design prevents performance issues and allows selective monitoring.

**Features**:

#### System Overview (Click "📈 Load Debug Stats")
- **Processing Metrics**: Total memory sessions, success rates, processing times
- **Failure Analysis**: Failed extractions and error tracking
- **Performance Monitoring**: Average processing times and bottlenecks
- **Live Statistics**: Real-time system performance data

#### Recent Memory Sessions (Click "📋 Load Recent Sessions")
- **Session Listing**: Recent memory processing attempts with status
- **Session Details**: Deep dive into specific processing sessions with full JSON data
- **Pipeline Tracing**: Step-by-step processing flow analysis
- **Error Debugging**: Detailed error messages and stack traces for failed sessions

#### System Configuration (Click "📋 Load Memory Config")
- **Memory Config**: Current memory extraction settings and LLM configuration
- **Debug Settings**: System debug mode, logging levels, and performance settings
- **Live Config**: Real-time configuration without restart required

#### Failure Recovery System (Click "📊 Load System Overview")
- **System Health**: Overall system status (healthy/degraded/critical)
- **Queue Statistics**: Processing queue metrics, backlogs, and throughput
- **Service Health**: Real-time health checks for all dependencies:
  - MongoDB connectivity and response times
  - Qdrant vector database status and performance
  - Ollama/OpenAI API availability and model status
  - ASR service connectivity and transcription status
- **Recovery Metrics**: Automatic recovery attempts and success rates

#### Service Health Monitoring (Click "🔍 Check Service Health")
- **Service Status Grid**: Visual status indicators for all services
- **Response Times**: Real-time latency metrics for each service
- **Failure Tracking**: Consecutive failure counts and error messages
- **Circuit Breaker Status**: Service protection states and thresholds

#### Usage Tips
- **Click buttons to load data**: Content appears only after clicking section buttons
- **Refresh data**: Use the "🔄 Refresh Debug Data" button to clear cache and reload
- **Monitor continuously**: Regularly check different sections for system health
- **Error investigation**: Use session details to debug processing failures

## API Integration

### Debug API Endpoints (Admin)
The dashboard integrates with comprehensive debug APIs:

**Memory Debug APIs:**
- `GET /api/debug/memory/stats` - Processing statistics
- `GET /api/debug/memory/sessions` - Recent sessions
- `GET /api/debug/memory/session/{uuid}` - Session details
- `GET /api/debug/memory/config` - Configuration
- `GET /api/debug/memory/pipeline/{uuid}` - Pipeline trace

**Failure Recovery APIs:**
- `GET /api/failure-recovery/system-overview` - System status
- `GET /api/failure-recovery/queue-stats` - Queue metrics
- `GET /api/failure-recovery/health` - Service health
- `GET /api/failure-recovery/circuit-breakers` - Circuit breaker status

### Authentication Requirements
- All debug APIs require admin authentication
- JWT tokens must have `is_superuser: true`
- Regular users see filtered data based on their user ID

## Advanced Features

### Real-time Updates
- **Auto-refresh**: Configurable refresh intervals for live data
- **WebSocket Status**: Live connection monitoring
- **Health Monitoring**: Real-time service status updates

### Data Export
- **Memory Export**: Download memories in JSON format
- **Conversation Export**: Export transcripts and audio metadata
- **Debug Reports**: Export system performance reports

### Troubleshooting Tools
- **Connection Testing**: Verify backend API connectivity
- **Authentication Debugging**: Token validation and user info display
- **Service Diagnostics**: Health checks for all system components

## Configuration

### Environment Variables
```bash
# Backend API connection
BACKEND_API_URL=http://localhost:8000
BACKEND_PUBLIC_URL=http://your-domain:8000

# Debug mode
DEBUG=true  # Enable detailed logging

# Authentication (inherited from backend)
AUTH_SECRET_KEY=your-secret-key
ADMIN_PASSWORD=your-admin-password
```

## Usage Patterns

### Admin Workflow
1. **Login** with superuser credentials
2. **System Health**: Check debug logs tab for service status
3. **Monitor Processing**: Review memory debug statistics
4. **User Management**: Create/manage user accounts as needed
5. **Troubleshooting**: Use debug tools to investigate issues

### User Workflow
1. **Authentication**: Login via sidebar
2. **View Conversations**: Browse recent audio sessions
3. **Search Memories**: Find relevant conversation insights
5. **Connect Clients**: Use provided tokens for audio devices

## Security Considerations

### Access Control
- **Role-based UI**: Admin features hidden from regular users
- **API Security**: All requests include proper authentication headers
- **Token Management**: Secure token storage and automatic refresh

### Data Privacy
- **User Isolation**: Non-admin users only see their own data
- **Audit Logging**: All admin actions logged for accountability
- **Secure Communication**: HTTPS recommended for production

## Troubleshooting

### Common Issues

#### Connection Problems
- Verify `BACKEND_API_URL` points to running backend
- Check firewall/port settings
- Ensure backend health endpoint responds

#### Authentication Failures
- Verify admin credentials in backend `.env`
- Check JWT token expiration (1-hour default)
- Confirm user has appropriate permissions

#### Missing Debug Tab
- Only visible to admin users (`is_superuser: true`)
- Verify authentication with admin account
- Check backend user creation and superuser flag

#### API Errors
- Check backend logs for detailed error information
- Verify all required services are running (MongoDB, Qdrant, etc.)
- Test API endpoints directly with curl for debugging

### Debug Steps
1. **Check Logs**: `./logs/streamlit.log` for frontend issues
2. **Backend Health**: Use `/health` endpoint to verify backend status
3. **API Testing**: Test endpoints directly with admin token
4. **Service Status**: Use debug tab to check component health
5. **Configuration**: Verify all environment variables are set correctly

This dashboard provides comprehensive system management capabilities with particular strength in debugging and monitoring the audio processing pipeline and memory extraction systems.
