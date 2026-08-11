// Generated from cmux-tui/spec/sdk-schema.json. DO NOT EDIT.
package com.cmux.raw;


import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;


/** Immutable report-agent request. Protocol v6; authority: control. */
public final class ReportAgentRequest implements WireValue {
    private final Field<UInt64> agentsActive;
    private final Field<String> detail;
    private final Field<UInt64> jobsRunning;
    private final Field<String> label;
    private final Field<Boolean> rootSession;
    private final Field<String> session;
    private final AgentReportSource source;
    private final Field<UInt64> startedAtMs;
    private final AgentState state;
    private final UInt64 surface;
    private final Field<UInt64> tasksCompleted;
    private final Field<UInt64> tasksTotal;

    private ReportAgentRequest(Builder builder) {
        this.agentsActive = builder.agentsActive;
        this.detail = builder.detail;
        this.jobsRunning = builder.jobsRunning;
        this.label = builder.label;
        this.rootSession = builder.rootSession;
        this.session = builder.session;
        if (!builder.sourceSet) throw new IllegalArgumentException("source is required");
        this.source = Wire.nonNull(builder.source, "source");
        this.startedAtMs = builder.startedAtMs;
        if (!builder.stateSet) throw new IllegalArgumentException("state is required");
        this.state = Wire.nonNull(builder.state, "state");
        if (!builder.surfaceSet) throw new IllegalArgumentException("surface is required");
        this.surface = Wire.nonNull(builder.surface, "surface");
        this.tasksCompleted = builder.tasksCompleted;
        this.tasksTotal = builder.tasksTotal;
    }

    public static Builder builder() { return new Builder(); }

    public Field<UInt64> agentsActive() { return agentsActive; }
    public Field<String> detail() { return detail; }
    public Field<UInt64> jobsRunning() { return jobsRunning; }
    public Field<String> label() { return label; }
    public Field<Boolean> rootSession() { return rootSession; }
    public Field<String> session() { return session; }
    public AgentReportSource source() { return source; }
    public Field<UInt64> startedAtMs() { return startedAtMs; }
    public AgentState state() { return state; }
    public UInt64 surface() { return surface; }
    public Field<UInt64> tasksCompleted() { return tasksCompleted; }
    public Field<UInt64> tasksTotal() { return tasksTotal; }

    public static ReportAgentRequest fromWire(Object value) {
        Map<String, Object> object = Wire.object(value, "ReportAgentRequest");
        Builder builder = builder();
        Object rawAgentsActive = Wire.optional(object, "agents_active");
        if (!Wire.isMissing(rawAgentsActive)) {
            builder.agentsActive(rawAgentsActive == null ? null : Wire.uint64(rawAgentsActive, "ReportAgentRequest.agents_active"));
        }
        Object rawDetail = Wire.optional(object, "detail");
        if (!Wire.isMissing(rawDetail)) {
            builder.detail(rawDetail == null ? null : Wire.string(rawDetail, "ReportAgentRequest.detail"));
        }
        Object rawJobsRunning = Wire.optional(object, "jobs_running");
        if (!Wire.isMissing(rawJobsRunning)) {
            builder.jobsRunning(rawJobsRunning == null ? null : Wire.uint64(rawJobsRunning, "ReportAgentRequest.jobs_running"));
        }
        Object rawLabel = Wire.optional(object, "label");
        if (!Wire.isMissing(rawLabel)) {
            builder.label(rawLabel == null ? null : Wire.string(rawLabel, "ReportAgentRequest.label"));
        }
        Object rawRootSession = Wire.optional(object, "root_session");
        if (!Wire.isMissing(rawRootSession)) {
            builder.rootSession(Wire.bool(rawRootSession, "ReportAgentRequest.root_session"));
        }
        Object rawSession = Wire.optional(object, "session");
        if (!Wire.isMissing(rawSession)) {
            builder.session(rawSession == null ? null : Wire.string(rawSession, "ReportAgentRequest.session"));
        }
        Object rawSource = Wire.required(object, "source");
        builder.source(AgentReportSource.fromWire(rawSource));
        Object rawStartedAtMs = Wire.optional(object, "started_at_ms");
        if (!Wire.isMissing(rawStartedAtMs)) {
            builder.startedAtMs(rawStartedAtMs == null ? null : Wire.uint64(rawStartedAtMs, "ReportAgentRequest.started_at_ms"));
        }
        Object rawState = Wire.required(object, "state");
        builder.state(AgentState.fromWire(rawState));
        Object rawSurface = Wire.required(object, "surface");
        builder.surface(Wire.uint64(rawSurface, "ReportAgentRequest.surface"));
        Object rawTasksCompleted = Wire.optional(object, "tasks_completed");
        if (!Wire.isMissing(rawTasksCompleted)) {
            builder.tasksCompleted(rawTasksCompleted == null ? null : Wire.uint64(rawTasksCompleted, "ReportAgentRequest.tasks_completed"));
        }
        Object rawTasksTotal = Wire.optional(object, "tasks_total");
        if (!Wire.isMissing(rawTasksTotal)) {
            builder.tasksTotal(rawTasksTotal == null ? null : Wire.uint64(rawTasksTotal, "ReportAgentRequest.tasks_total"));
        }
        return builder.build();
    }

    @Override
    public Map<String, Object> toWire() {
        LinkedHashMap<String, Object> object = new LinkedHashMap<>();
        Wire.put(object, "agents_active", agentsActive);
        Wire.put(object, "detail", detail);
        Wire.put(object, "jobs_running", jobsRunning);
        Wire.put(object, "label", label);
        Wire.put(object, "root_session", rootSession);
        Wire.put(object, "session", session);
        Wire.put(object, "source", source);
        Wire.put(object, "started_at_ms", startedAtMs);
        Wire.put(object, "state", state);
        Wire.put(object, "surface", surface);
        Wire.put(object, "tasks_completed", tasksCompleted);
        Wire.put(object, "tasks_total", tasksTotal);
        return Collections.unmodifiableMap(object);
    }

    @Override
    public boolean equals(Object other) {
        if (!(other instanceof ReportAgentRequest that)) return false;
        return Objects.equals(agentsActive, that.agentsActive) && Objects.equals(detail, that.detail) && Objects.equals(jobsRunning, that.jobsRunning) && Objects.equals(label, that.label) && Objects.equals(rootSession, that.rootSession) && Objects.equals(session, that.session) && Objects.equals(source, that.source) && Objects.equals(startedAtMs, that.startedAtMs) && Objects.equals(state, that.state) && Objects.equals(surface, that.surface) && Objects.equals(tasksCompleted, that.tasksCompleted) && Objects.equals(tasksTotal, that.tasksTotal);
    }

    @Override
    public int hashCode() { return Objects.hash(agentsActive, detail, jobsRunning, label, rootSession, session, source, startedAtMs, state, surface, tasksCompleted, tasksTotal); }

    @Override
    public String toString() { return "ReportAgentRequest" + toWire(); }

    public static final class Builder {
        private Field<UInt64> agentsActive = Field.omitted();
        private Field<String> detail = Field.omitted();
        private Field<UInt64> jobsRunning = Field.omitted();
        private Field<String> label = Field.omitted();
        private Field<Boolean> rootSession = Field.omitted();
        private Field<String> session = Field.omitted();
        private AgentReportSource source;
        private boolean sourceSet;
        private Field<UInt64> startedAtMs = Field.omitted();
        private AgentState state;
        private boolean stateSet;
        private UInt64 surface;
        private boolean surfaceSet;
        private Field<UInt64> tasksCompleted = Field.omitted();
        private Field<UInt64> tasksTotal = Field.omitted();

        public Builder agentsActive(UInt64 value) {
            this.agentsActive = Field.ofNullable(value);
            return this;
        }
        public Builder detail(String value) {
            this.detail = Field.ofNullable(value);
            return this;
        }
        public Builder jobsRunning(UInt64 value) {
            this.jobsRunning = Field.ofNullable(value);
            return this;
        }
        public Builder label(String value) {
            this.label = Field.ofNullable(value);
            return this;
        }
        public Builder rootSession(Boolean value) {
            this.rootSession = Field.of(value);
            return this;
        }
        public Builder session(String value) {
            this.session = Field.ofNullable(value);
            return this;
        }
        public Builder source(AgentReportSource value) {
            this.source = value;
            this.sourceSet = true;
            return this;
        }
        public Builder startedAtMs(UInt64 value) {
            this.startedAtMs = Field.ofNullable(value);
            return this;
        }
        public Builder state(AgentState value) {
            this.state = value;
            this.stateSet = true;
            return this;
        }
        public Builder surface(UInt64 value) {
            this.surface = value;
            this.surfaceSet = true;
            return this;
        }
        public Builder tasksCompleted(UInt64 value) {
            this.tasksCompleted = Field.ofNullable(value);
            return this;
        }
        public Builder tasksTotal(UInt64 value) {
            this.tasksTotal = Field.ofNullable(value);
            return this;
        }
        public ReportAgentRequest build() { return new ReportAgentRequest(this); }
    }
}
