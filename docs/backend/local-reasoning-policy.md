# Local model reasoning policy

Local thinking models (`models[].thinking: true`) default to `reasoning_policy: "off"`. The capability flag describes what the server supports; it does not authorize hidden reasoning. The policy overrides all operation efforts, model parameter defaults, Pi backend settings and Timeline stage overrides.

```yaml
models:
  - name: qwen3.8-llm
    thinking: true
    reasoning_policy: "off"
```

Quote `"off"` in YAML. Unquoted `off` may parse as a boolean.

To deliberately use local reasoning, set that model's `reasoning_policy: per_operation` and explicitly configure a nonzero operation `reasoning_effort`. A missing operation or omitted effort remains off, even if `model_params.reasoning_effort` is high. Remote provider operation settings and Codex CLI reasoning remain separately configured.

`ResolvedLLMOperation.effective_reasoning_effort` is the source used for API parameters. A disabled local request always contains `chat_template_kwargs.enable_thinking: false`, including unconfigured operations, fallback routing and the admin model test. Unnamed `async_generate` requests resolve the `default` registry operation.

Pi carries the model permission into its immutable runtime configuration. Construction and replacement keep thinking off when permission is absent. Its provider-request extension enforces the false chat-template flag after the harness creates the payload, so adapter compatibility and stage effort overrides cannot re-enable it.

Model-route diagnostics distinguish requested effort, effective effort and the model policy. This prevents an operation configured as high from being reported as effectively high while disabled by the model.

Regression tests exercise the default operation inventory, newly named operations, explicit low/high requests, model defaults, fallback models, unnamed generation, Pi resolution, stage overrides, outgoing Pi payload and the admin probe. Intentional reasoning tests must explicitly opt in. This does not promise that requests cannot exhaust output/context budgets for other reasons.
