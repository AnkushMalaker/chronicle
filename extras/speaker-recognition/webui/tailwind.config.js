/** @type {import('tailwindcss').Config} */
import espressoPreset from './chronicle-espresso-preset.js'

export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  presets: [espressoPreset],
  theme: {
    extend: {},
  },
  plugins: [],
}
