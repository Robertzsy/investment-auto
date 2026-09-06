"""Paper-trading control, risk and execution services.

The AI decision layer lives in the DSH app (Investment Auto 2.0); this
package owns the non-bypassable boundaries: trading controls (pause/resume/
kill), hard risk, and the paper broker. Autonomous cycle execution is driven
by the DSH bridge through the pluggable scheduler runner (see engine.scheduler).
"""
