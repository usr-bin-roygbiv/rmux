// Generated from cmux-tui/spec/sdk-schema.json. DO NOT EDIT.
package com.cmux.raw;


import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;


/** Immutable surface-exited event. Protocol v5; streams: subscribe. */
public final class SurfaceExitedEvent implements WireValue, DeltaStreamEvent, ProtocolEvent, SubscribeEvent {
    /** Hosted child runtime in milliseconds; null for browser and non-hosted surfaces. */
    private final Field<UInt64> runtimeMs;
    private final UInt64 surface;

    private SurfaceExitedEvent(Builder builder) {
        this.runtimeMs = builder.runtimeMs;
        if (!builder.surfaceSet) throw new IllegalArgumentException("surface is required");
        this.surface = Wire.nonNull(builder.surface, "surface");
    }

    public static Builder builder() { return new Builder(); }

    public Field<UInt64> runtimeMs() { return runtimeMs; }
    public UInt64 surface() { return surface; }
    @Override public String event() { return "surface-exited"; }

    public static SurfaceExitedEvent fromWire(Object value) {
        Map<String, Object> object = Wire.object(value, "SurfaceExitedEvent");
        Builder builder = builder();
        ProtocolSupport.literal(Wire.required(object, "event"), "surface-exited", "SurfaceExitedEvent.event");
        Object rawRuntimeMs = Wire.optional(object, "runtime_ms");
        if (!Wire.isMissing(rawRuntimeMs)) {
            builder.runtimeMs(rawRuntimeMs == null ? null : Wire.uint64(rawRuntimeMs, "SurfaceExitedEvent.runtime_ms"));
        }
        Object rawSurface = Wire.required(object, "surface");
        builder.surface(Wire.uint64(rawSurface, "SurfaceExitedEvent.surface"));
        return builder.build();
    }

    @Override
    public Map<String, Object> toWire() {
        LinkedHashMap<String, Object> object = new LinkedHashMap<>();
        object.put("event", "surface-exited");
        Wire.put(object, "runtime_ms", runtimeMs);
        Wire.put(object, "surface", surface);
        return Collections.unmodifiableMap(object);
    }

    @Override
    public boolean equals(Object other) {
        if (!(other instanceof SurfaceExitedEvent that)) return false;
        return Objects.equals(runtimeMs, that.runtimeMs) && Objects.equals(surface, that.surface);
    }

    @Override
    public int hashCode() { return Objects.hash(runtimeMs, surface); }

    @Override
    public String toString() { return "SurfaceExitedEvent" + toWire(); }

    public static final class Builder {
        private Field<UInt64> runtimeMs = Field.omitted();
        private UInt64 surface;
        private boolean surfaceSet;

        public Builder runtimeMs(UInt64 value) {
            this.runtimeMs = Field.ofNullable(value);
            return this;
        }
        public Builder surface(UInt64 value) {
            this.surface = value;
            this.surfaceSet = true;
            return this;
        }
        public SurfaceExitedEvent build() { return new SurfaceExitedEvent(this); }
    }
}
