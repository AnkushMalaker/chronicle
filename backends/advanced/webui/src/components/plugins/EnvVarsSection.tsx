import { Key } from 'lucide-react'
import FormField, { FieldSchema } from './FormField'

interface EnvVarsSectionProps {
  schema: Record<string, FieldSchema>
  values: Record<string, any>
  onChange: (envVars: Record<string, any>) => void
  errors?: Record<string, string>
  disabled?: boolean
}

export default function EnvVarsSection({
  schema,
  values,
  onChange,
  errors = {},
  disabled = false
}: EnvVarsSectionProps) {
  const envVarKeys = Object.keys(schema)

  if (envVarKeys.length === 0) {
    return null
  }

  const handleChange = (key: string, value: any) => {
    onChange({
      ...values,
      [key]: value
    })
  }

  return (
    <div className="space-y-4">
      {/* Section Header */}
      <div className="flex items-center space-x-2 pb-2 border-b border-gray-200 dark:border-gray-700">
        <Key className="h-5 w-5 text-blue-600" />
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Secrets & Environment Variables
        </h3>
      </div>

      <p className="text-sm text-gray-600 dark:text-gray-400">
        Environment variables and secrets for this plugin. Values are stored securely and masked for display.
      </p>

      <div className="space-y-4">
        {envVarKeys.map((key) => {
          const fieldSchema = schema[key]
          const value = values[key]
          const error = errors[key]

          return (
            <div
              key={key}
              className="p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg"
            >
              <FormField
                fieldKey={key}
                schema={fieldSchema}
                value={value}
                onChange={(newValue) => handleChange(key, newValue)}
                error={error}
                disabled={disabled}
              />

              {fieldSchema.env_var && (
                <div className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  <span className="font-mono bg-gray-200 dark:bg-gray-600 px-2 py-0.5 rounded">
                    ${fieldSchema.env_var}
                  </span>
                  {fieldSchema.secret && (
                    <span className="ml-2 text-yellow-600 dark:text-yellow-400">
                      🔒 Stored securely in .env file
                    </span>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="p-3 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg">
        <p className="text-xs text-yellow-800 dark:text-yellow-200">
          <strong>Note:</strong> Changing environment variables requires a backend restart to take effect.
          Existing values are masked with <code className="font-mono">••••••••</code> for security.
        </p>
      </div>
    </div>
  )
}
