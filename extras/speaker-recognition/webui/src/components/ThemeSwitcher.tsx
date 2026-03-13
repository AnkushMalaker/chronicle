import { useTheme } from "../contexts/ThemeContext";
import { Moon, Sun } from 'lucide-react';

export const ThemeSwitcher = () => {
    const { isDark, toggleTheme } = useTheme();
    return <button
        onClick={toggleTheme}
        className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-gray-600 dark:text-gray-300"
    >
        {isDark ? <Moon className="" /> : <Sun className="" />}
    </button>;
}
