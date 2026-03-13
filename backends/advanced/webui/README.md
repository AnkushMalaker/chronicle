# Chronicle Web Dashboard

A modern React-based web interface for the Chronicle AI-powered personal audio system.

## Features

- **Modern React Stack**: Built with React 18, TypeScript, and Vite
- **Responsive Design**: TailwindCSS with dark/light theme support
- **JWT Authentication**: Secure login with role-based access control
- **Real-time Updates**: Live system monitoring and health checks
- **Admin Features**: User management, system monitoring, and audio file upload
- **Audio Management**: Conversation viewing with playback support
- **Memory System**: Search and manage extracted memories
- **Mobile Friendly**: Responsive design that works on all devices

## Pages

### Public Pages
- **Login** (`/login`) - Authentication page

### Authenticated Pages
- **Conversations** (`/conversations`) - View and manage conversations with audio playback
- **Memories** (`/memories`) - Search and manage extracted memories
- **User Management** (`/users`) - View user accounts (admin: full CRUD)

### Admin-Only Pages
- **System Monitoring** (`/system`) - Health checks, metrics, and active clients
- **Upload** (`/upload`) - Bulk audio file upload and processing

## Development

### Prerequisites
- Node.js 18+
- Docker (for backend services)

### Local Development

1. **Install dependencies:**
   ```bash
   cd backends/advanced/webui
   npm install
   ```

2. **Set up environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your backend URL
   ```

3. **Start development server:**
   ```bash
   npm run dev
   ```

   The app will be available at http://localhost:5173

4. **Start backend services:**
   ```bash
   cd ../
   docker compose up chronicle-backend mongo qdrant
   ```

### Docker Development

Use the development profile to run both frontend and backend in Docker:

```bash
cd backends/advanced
docker compose --profile dev up
```

This starts:
- Backend services (chronicle-backend, mongo, qdrant)
- React dev server with hot reload (http://localhost:5173)

## Production Deployment

### Docker Compose (Recommended)

```bash
cd backends/advanced
docker compose up webui chronicle-backend mongo qdrant
```

The production build will be available at http://localhost:3000

### Manual Build

```bash
cd backends/advanced/webui-react
npm install
npm run build
# Serve the dist/ folder with any web server
```

## Configuration

### Environment Variables

- `VITE_BACKEND_URL` - Backend API URL (default: http://localhost:8000)

### Docker Compose Variables

- `WEBUI_PORT` - Production webui port (default: 3000)
- `WEBUI_DEV_PORT` - Development webui port (default: 5173)
- `HOST_IP` - Your host IP for external access
- `BACKEND_PUBLIC_PORT` - Backend port accessible from browser

## Architecture

### Frontend Stack
- **React 18** - UI framework with hooks and concurrent features
- **TypeScript** - Type safety and better developer experience
- **Vite** - Fast build tool with HMR
- **TailwindCSS** - Utility-first CSS framework
- **React Router** - Client-side routing
- **Axios** - HTTP client with interceptors
- **Lucide React** - Modern icon library

### Authentication
- JWT token-based authentication
- Automatic token refresh and validation
- Role-based access control (admin vs user)
- Protected routes with loading states

### API Integration
- Centralized API client with error handling
- Request/response interceptors for auth tokens
- Automatic logout on token expiration
- Loading states and error management

## File Structure

```
webui-react/
├── public/                 # Static assets
├── src/
│   ├── components/         # Reusable components
│   │   ├── auth/          # Authentication components
│   │   └── layout/        # Layout components
│   ├── contexts/          # React contexts
│   │   ├── AuthContext.tsx
│   │   └── ThemeContext.tsx
│   ├── pages/             # Page components
│   │   ├── Conversations.tsx
│   │   ├── Memories.tsx
│   │   ├── Users.tsx
│   │   ├── System.tsx
│   │   ├── Upload.tsx
│   │   └── LoginPage.tsx
│   ├── services/          # API services
│   │   └── api.ts
│   ├── App.tsx            # Root component
│   ├── main.tsx           # Entry point
│   └── index.css          # Global styles
├── Dockerfile             # Production build
├── Dockerfile.dev         # Development build
├── nginx.conf             # Nginx configuration
└── package.json
```

## API Endpoints

The frontend integrates with these backend endpoints:

### Authentication
- `POST /auth/jwt/login` - User login
- `GET /auth/me` - Get current user info

### Conversations
- `GET /api/conversations` - List conversations
- `GET /api/conversations/{id}` - Get conversation details
- `DELETE /api/conversations/{id}` - Delete conversation

### Memories
- `GET /api/memories` - Get user memories (filtered)
- `GET /api/memories/unfiltered` - Get user memories (unfiltered)
- `DELETE /api/memories/{id}` - Delete memory

### Users (Admin)
- `GET /api/users` - List all users
- `POST /api/users` - Create user
- `PUT /api/users/{id}` - Update user
- `DELETE /api/users/{id}` - Delete user

### System (Admin)
- `GET /health` - Basic health check
- `GET /readiness` - Service readiness check
- `GET /api/metrics` - System metrics
- `GET /api/processor/status` - Processor status
- `GET /api/clients/active` - Active WebSocket clients

### Upload (Admin)
- `POST /api/audio/upload` - Upload and process audio files

## Deployment Notes

### Production Considerations
- Environment variables are built into the bundle at build time
- Use proper `VITE_BACKEND_URL` for your production backend
- Enable HTTPS in production environments
- Configure proper CORS settings on the backend

### Docker Deployment
- Production image uses multi-stage build for smaller size
- Nginx serves the static files and handles routing
- Includes security headers and gzip compression
- Development image includes hot reload for easier development

## Troubleshooting

### Common Issues

1. **CORS Errors**: Ensure backend CORS settings allow your frontend domain
2. **Authentication Issues**: Check JWT token validity and backend auth endpoints
3. **API Connection**: Verify `VITE_BACKEND_URL` matches your backend location
4. **Docker Build Issues**: Clear Docker cache and rebuild from scratch

### Development Tips
- Use browser dev tools to inspect network requests
- Check console for React/TypeScript errors
- Use Docker logs to debug backend connectivity
- Verify environment variables are loaded correctly
