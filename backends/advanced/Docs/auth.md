# Authentication Architecture

## Overview

Chronicle uses a comprehensive authentication system built on `fastapi-users` with support for multiple authentication methods including JWT tokens and cookies. The system provides secure user management with proper data isolation and role-based access control using MongoDB ObjectIds for user identification.

## Architecture Components

### 1. User Model (`users.py`)

```python
class User(BeanieBaseUser, Document):
    # Standard fastapi-users fields
    email: str
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = False
    is_verified: bool = False
    
    # Custom fields
    display_name: Optional[str] = None
    registered_clients: dict[str, dict] = Field(default_factory=dict)
    
    @property
    def user_id(self) -> str:
        """Return string representation of MongoDB ObjectId for backward compatibility."""
        return str(self.id)
```

**Key Features:**
- **Email-based Authentication**: Users authenticate using email addresses
- **MongoDB ObjectId**: Uses MongoDB's native ObjectId as unique identifier
- **MongoDB Integration**: Uses Beanie ODM for document storage
- **Backward Compatibility**: user_id property provides ObjectId as string

### 2. Authentication Manager (`auth.py`)

```python
class UserManager(BaseUserManager[User, PydanticObjectId]):
    async def authenticate(self, credentials: dict) -> Optional[User]:
        """Authenticate with email+password"""
        username = credentials.get("username")
        # Email-based authentication only
        
    async def get_by_email(self, email: str) -> Optional[User]:
        """Get user by email address"""
```

**Key Features:**
- **Email Authentication**: Login with email address only
- **Password Management**: Secure password hashing and verification
- **Standard fastapi-users**: Uses standard user creation without custom IDs
- **MongoDB ObjectId**: Relies on MongoDB's native unique ID generation

### 3. Authentication Backends

#### JWT Bearer Token
- **Endpoint**: `/auth/jwt/login`
- **Transport**: Authorization header (`Bearer <token>`)
- **Lifetime**: 1 hour
- **Usage**: API calls, WebSocket authentication

#### Cookie Authentication
- **Endpoint**: `/auth/cookie/login`
- **Transport**: HTTP cookies
- **Lifetime**: 1 hour
- **Usage**: Web dashboard, browser-based clients


## Authentication Flow

### 1. User Registration

**Admin-Only Registration:**
```bash
# Create user with auto-generated MongoDB ObjectId
curl -X POST "http://localhost:8000/api/create_user" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "userpass",
    "display_name": "John Doe"
  }'

# Response: {"id": "507f1f77bcf86cd799439011", "email": "user@example.com", ...}
```

### 2. Authentication Methods

#### Email-based Login
```bash
curl -X POST "http://localhost:8000/auth/jwt/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=user@example.com&password=userpass"
```


### 3. WebSocket Authentication

#### Token-based (Recommended)
```javascript
const ws = new WebSocket('ws://localhost:8000/ws?codec=pcm?token=JWT_TOKEN&device_name=phone');
```

#### Cookie-based
```javascript
// Requires existing cookie from web login
const ws = new WebSocket('ws://localhost:8000/ws?codec=pcm?device_name=phone');
```

## Client ID Management

### Format: `user_id_suffix-device_name`

The system automatically generates client IDs by combining:
- **user_id_suffix**: Last 6 characters of MongoDB ObjectId
- **device_name**: Sanitized device identifier

**Examples:**
- `a39011-phone` (user ObjectId ending in a39011, device: phone)
- `cd7994-desktop` (user ObjectId ending in cd7994, device: desktop)
- `f86cd7-havpe` (user ObjectId ending in f86cd7, device: havpe)

### Benefits:
- **User Association**: Clear mapping between clients and users
- **Device Tracking**: Multiple devices per user
- **Data Isolation**: Each user only accesses their own data
- **Audit Trail**: Track activity by user and device
- **Unique IDs**: MongoDB ObjectId ensures global uniqueness

## Security Features

### 1. Password Security
- **Bcrypt Hashing**: Secure password storage with salt
- **Password Verification**: Constant-time comparison
- **Hash Updates**: Automatic rehashing on login when needed

### 2. Token Security
- **JWT Tokens**: Signed with secret key
- **Short Lifetime**: 1-hour expiration
- **Secure Transport**: HTTPS recommended for production

### 3. Data Isolation
- **User Scoping**: All data scoped to user's MongoDB ObjectId
- **Client Filtering**: Users only see their own clients
- **Admin Override**: Superusers can access all data

### 4. WebSocket Security
- **Authentication Required**: All WebSocket connections require auth
- **Token Validation**: JWT tokens validated on connection
- **Graceful Rejection**: Unauthenticated connections rejected with reason

## Environment Configuration

### Required Variables
```bash
# JWT secret key (minimum 32 characters)
AUTH_SECRET_KEY=your-super-secret-jwt-key-here-make-it-long-and-random

# Admin user credentials
ADMIN_PASSWORD=your-secure-admin-password
ADMIN_EMAIL=admin@example.com
```

### Optional Variables
```bash
# Cookie security (set to true for HTTPS)
COOKIE_SECURE=false

```

## API Endpoints

### Authentication
- `POST /auth/jwt/login` - JWT token authentication
- `POST /auth/cookie/login` - Cookie-based authentication
- `POST /auth/logout` - Logout (clear cookies)

### User Management
- `POST /api/create_user` - Create new user (admin only)
- `GET /api/users/me` - Get current user info
- `PATCH /api/users/me` - Update user profile

### WebSocket Endpoints
- `ws://host/ws?codec=opus` - Opus audio stream with auth
- `ws://host/ws?codec=pcm` - PCM audio stream with auth (default)

## Error Handling

### Authentication Errors
- **401 Unauthorized**: Invalid credentials or expired token
- **403 Forbidden**: Insufficient permissions
- **422 Validation Error**: Invalid request format

### WebSocket Errors
- **1008 Policy Violation**: Authentication required
- **1011 Server Error**: Internal authentication error

## Best Practices

### 1. Client Implementation
```python
# Use email for authentication
AUTH_USERNAME = "user@example.com"  # Email address
AUTH_PASSWORD = "secure_password"

# Use single AUTH_USERNAME variable for email authentication
```

### 2. Token Management
```python
# Store tokens securely
# Refresh tokens before expiry
# Handle 401 errors gracefully
```

### 3. Production Deployment
```bash
# Use strong secrets
AUTH_SECRET_KEY=$(openssl rand -base64 32)

# Enable HTTPS
COOKIE_SECURE=true

# Use environment variables
# Never commit secrets to version control
```

### 4. Admin User Setup
```bash
# Create admin during startup
ADMIN_PASSWORD=secure_admin_password
ADMIN_EMAIL=admin@yourdomain.com
```

## Troubleshooting

### Common Issues

#### 1. Authentication Failures
```bash
# Check credentials
curl -X POST "http://localhost:8000/auth/jwt/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=user@example.com&password=test"

# Verify user exists by email
# Check password is correct
# Ensure user is active
```

#### 2. WebSocket Connection Issues
```javascript
// Check token validity
// Verify URL format
// Test with curl first
```

#### 3. Admin User Creation
```bash
# Check logs for admin creation
docker compose logs chronicle-backend | grep -i admin

# Verify environment variables
echo $ADMIN_PASSWORD
```

### Debug Commands
```bash
# Check user database
docker exec -it mongo-container mongosh chronicle

# View authentication logs
docker compose logs chronicle-backend | grep -i auth

# Test API endpoints
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/users/me
```

## Migration Guide

### From Custom user_id to Email-Only Authentication

1. **Update Environment Variables**
   ```bash
   # Old
   AUTH_USERNAME=abc123  # Custom user_id (deprecated)
   
   # New
   AUTH_USERNAME=user@example.com  # Email address only
   ```

2. **Update Client Code**
   ```python
   # Old
   username = AUTH_USERNAME  # Could be email or user_id
   
   # New
   username = AUTH_USERNAME  # Email address only
   ```

3. **Test Authentication**
   ```bash
   # Only email authentication works now
   curl -X POST "http://localhost:8000/auth/jwt/login" \
     -d "username=user@example.com&password=pass"
   ```

## Advanced Features

### 1. Role-Based Access Control
```python
# Regular user - can only access own data
@app.get("/api/data")
async def get_data(user: User = Depends(current_active_user)):
    return get_user_data(user.user_id)

# Admin user - can access all data
@app.get("/api/admin/data")
async def get_all_data(user: User = Depends(current_superuser)):
    return get_all_data()
```

### 2. OAuth Integration
```python
# Automatic email verification for OAuth users
```

### 3. Multi-Device Support
```python
# Single user, multiple devices
# Client IDs: a39011-phone, cd7994-tablet, f86cd7-desktop
# Separate conversation streams per device
# Unified user dashboard
```

This authentication system provides enterprise-grade security with developer-friendly APIs, supporting email/password authentication and modern OAuth flows while maintaining proper data isolation and user management capabilities using MongoDB's robust ObjectId system.