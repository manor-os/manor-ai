# Remotion core rules

- Build compositions from React components with synchronous `registerRoot()` registration.
- Use `useCurrentFrame()`, `useVideoConfig()`, `interpolate()`, `spring()`, and `<Sequence>` for deterministic timing.
- Include transition overlap in total duration calculations and clamp interpolation where values must not overshoot.
- Use `random(seed)` instead of `Math.random()`.
- Design for the composition's actual pixel dimensions and keep titles, captions, and calls to action inside readable safe areas.
- Use Remotion media components and local deterministic paths. Wait for fonts and asynchronous metadata using Remotion's render-delay APIs.
- Validate composition discovery and inspect stills near the beginning, scene transitions, and the end before rendering the requested output.
