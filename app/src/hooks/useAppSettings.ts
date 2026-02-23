import { useState, useEffect, useCallback } from 'react';
import {
  saveWebSocketUrl,
  getWebSocketUrl,
  saveUserId,
  getUserId,
  getAuthEmail,
  getJwtToken,
} from '../utils/storage';

export interface AppSettings {
  webSocketUrl: string;
  userId: string;
  isAuthenticated: boolean;
  currentUserEmail: string | null;
  jwtToken: string | null;
  handleSetAndSaveWebSocketUrl: (url: string) => Promise<void>;
  handleSetAndSaveUserId: (id: string) => Promise<void>;
  handleAuthStatusChange: (authenticated: boolean, email: string | null, token: string | null) => void;
}

export const useAppSettings = (): AppSettings => {
  const [webSocketUrl, setWebSocketUrl] = useState<string>('');
  const [userId, setUserId] = useState<string>('');
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [currentUserEmail, setCurrentUserEmail] = useState<string | null>(null);
  const [jwtToken, setJwtToken] = useState<string | null>(null);

  useEffect(() => {
    const loadSettings = async () => {
      const storedWsUrl = await getWebSocketUrl();
      if (storedWsUrl) {
        setWebSocketUrl(storedWsUrl);
      } else {
        const defaultUrl = 'ws://localhost:8000/ws';
        setWebSocketUrl(defaultUrl);
        await saveWebSocketUrl(defaultUrl);
      }

      const storedUserId = await getUserId();
      if (storedUserId) setUserId(storedUserId);

      const storedEmail = await getAuthEmail();
      const storedToken = await getJwtToken();
      if (storedEmail && storedToken) {
        setCurrentUserEmail(storedEmail);
        setJwtToken(storedToken);
        setIsAuthenticated(true);
      }
    };
    loadSettings();
  }, []);

  const handleSetAndSaveWebSocketUrl = useCallback(async (url: string) => {
    setWebSocketUrl(url);
    await saveWebSocketUrl(url);
  }, []);

  const handleSetAndSaveUserId = useCallback(async (id: string) => {
    setUserId(id);
    await saveUserId(id || null);
  }, []);

  const handleAuthStatusChange = useCallback((authenticated: boolean, email: string | null, token: string | null) => {
    setIsAuthenticated(authenticated);
    setCurrentUserEmail(email);
    setJwtToken(token);
  }, []);

  return {
    webSocketUrl,
    userId,
    isAuthenticated,
    currentUserEmail,
    jwtToken,
    handleSetAndSaveWebSocketUrl,
    handleSetAndSaveUserId,
    handleAuthStatusChange,
  };
};
