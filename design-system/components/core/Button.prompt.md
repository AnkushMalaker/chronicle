Chronicle button — one `primary` per view; `secondary` for toolbars; `danger` to delete; `ghost` for quiet actions.

```jsx
<Button variant="primary" icon={<Target size={14} />}>I'll say "hey hermes" now</Button>
<Button variant="secondary" icon={<RefreshCw size={14} />}>Refresh</Button>
<Button variant="danger">Delete audio</Button>
```
Sizes: `sm` (default, toolbar) · `md`. `disabled` drops to 40% opacity.
