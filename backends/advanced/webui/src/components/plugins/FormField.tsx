import { useState } from 'react'
import { AlertCircle, Eye, EyeOff } from 'lucide-react'

export interface FieldSchema {
  type: 'string' | 'number' | 'boolean' | 'password' | 'enum' | 'array' | 'object'
  label: string
  default?: any
  required?: boolean
  secret?: boolean
  env_var?: string
  min?: number
  max?: number
  help_text?: string
  options?: Array<{ value: string; label: string }>
}

interface FormFieldProps {
  fieldKey: string
  schema: FieldSchema
  value: any
  onChange: (value: any) => void
  error?: string
  disabled?: boolean
}

export default function FormField({
  fieldKey,
  schema,
  value,
  onChange,
  error,
  disabled = false
}: FormFieldProps) {
  const [showPassword, setShowPassword] = useState(false)
  const [isEditing, setIsEditing] = useState(false)

  const isMaskedValue = typeof value === 'string' && value.includes('••••')

  const renderField = () => {
    switch (schema.type) {
      case 'boolean':
        return (
          <div className="flex items-center space-x-2">
            <input
              type="checkbox"
              id={fieldKey}
              checked={value || false}
              onChange={(e) => onChange(e.target.checked)}
              disabled={disabled}
              className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded disabled:opacity-50"
            />
            <label
              htmlFor={fieldKey}
              className="text-sm text-gray-700 dark:text-gray-300"
            >
              {schema.label}
            </label>
          </div>
        )

      case 'number':
        return (
          <div>
            <label
              htmlFor={fieldKey}
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {schema.label}
              {schema.required && <span className="text-red-500 ml-1">*</span>}
            </label>
            <input
              type="number"
              id={fieldKey}
              value={value ?? schema.default ?? ''}
              onChange={(e) => onChange(e.target.valueAsNumber || parseInt(e.target.value))}
              min={schema.min}
              max={schema.max}
              disabled={disabled}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            />
            {schema.help_text && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {schema.help_text}
              </p>
            )}
          </div>
        )

      case 'password':
        const displayValue = isMaskedValue && !isEditing ? value : value || ''

        return (
          <div>
            <label
              htmlFor={fieldKey}
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {schema.label}
              {schema.required && <span className="text-red-500 ml-1">*</span>}
              {schema.env_var && (
                <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">
                  (${schema.env_var})
                </span>
              )}
            </label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                id={fieldKey}
                value={displayValue}
                onChange={(e) => {
                  setIsEditing(true)
                  onChange(e.target.value)
                }}
                onFocus={() => {
                  // When focusing on a masked field, clear it to allow entering new value
                  if (isMaskedValue && !isEditing) {
                    setIsEditing(true)
                    onChange('')
                  }
                }}
                disabled={disabled}
                placeholder={isMaskedValue ? 'Enter new password to change' : 'Enter password'}
                className="w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                title={showPassword ? 'Hide password' : 'Show password'}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 transition-colors"
                disabled={disabled}
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
              </button>
            </div>
            {schema.help_text && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {schema.help_text}
              </p>
            )}
            {isMaskedValue && !isEditing && (
              <p className="mt-1 text-xs text-blue-600 dark:text-blue-400">
                ✓ Password is set (hidden for security). Click to enter a new password.
              </p>
            )}
            {isEditing && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                💡 Click the <Eye className="inline h-3 w-3" /> icon to show/hide password while typing
              </p>
            )}
          </div>
        )

      case 'enum':
        return (
          <div>
            <label
              htmlFor={fieldKey}
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {schema.label}
              {schema.required && <span className="text-red-500 ml-1">*</span>}
            </label>
            <select
              id={fieldKey}
              value={value ?? schema.default ?? ''}
              onChange={(e) => onChange(e.target.value)}
              disabled={disabled}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {schema.options?.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {schema.help_text && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {schema.help_text}
              </p>
            )}
          </div>
        )

      case 'object': {
        const jsonStr = typeof value === 'string' ? value : JSON.stringify(value ?? schema.default ?? {}, null, 2)
        return (
          <div>
            <label
              htmlFor={fieldKey}
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {schema.label}
              {schema.required && <span className="text-red-500 ml-1">*</span>}
            </label>
            <textarea
              id={fieldKey}
              value={jsonStr}
              onChange={(e) => {
                try {
                  onChange(JSON.parse(e.target.value))
                } catch {
                  // Keep raw string while user is typing invalid JSON
                  onChange(e.target.value)
                }
              }}
              rows={Math.min(Math.max(jsonStr.split('\n').length, 3), 12)}
              disabled={disabled}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            />
            {schema.help_text && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {schema.help_text}
              </p>
            )}
          </div>
        )
      }

      case 'string':
      default:
        return (
          <div>
            <label
              htmlFor={fieldKey}
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              {schema.label}
              {schema.required && <span className="text-red-500 ml-1">*</span>}
            </label>
            <input
              type="text"
              id={fieldKey}
              value={value ?? schema.default ?? ''}
              onChange={(e) => onChange(e.target.value)}
              disabled={disabled}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
            />
            {schema.help_text && (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {schema.help_text}
              </p>
            )}
          </div>
        )
    }
  }

  return (
    <div className="space-y-1">
      {renderField()}
      {error && (
        <div className="flex items-start space-x-1 text-red-600 dark:text-red-400">
          <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
          <p className="text-xs">{error}</p>
        </div>
      )}
    </div>
  )
}
