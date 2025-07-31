# Development Roadmap

## Overview

The swf-monitor serves as the central hub for the ePIC streaming workflow testbed, driven by agent-based architecture with ActiveMQ messaging.

## Current System Architecture

```
swf-daqsim-agent (scheduler/generator)
    ↓ ActiveMQ messages
[swf-data-agent] → [swf-processing-agent] → [swf-fastmon-agent]
    ↓ status updates
swf-monitor (dashboard/database)
```

## Development Strategy

To avoid interfering with active agent development while establishing monitoring infrastructure:

1. **Create emulated agents** within the monitor repo as Django management commands
2. **Develop database schema** to track complete workflow pipeline
3. **Build monitoring views** to visualize workflow state and debug ActiveMQ traffic
4. **Design message protocol** for seamless real agent integration

## Roadmap Phases

### Phase 1: Database Schema Design

- [ ] Analyze STF messages from daqsim to understand data flow structure
- [ ] Design workflow tracking models:
  - STF lifecycle (received, processing, completed, failed)
  - Agent status and heartbeat tracking
  - Message dispatch history and error tracking
- [ ] Extend existing models (StfFile, MessageQueueDispatch) for workflow support
- [ ] Create Django migrations for schema changes

### Phase 2: Agent Emulation

- [ ] Create `emulate_data_agent` management command:
  - Listen for STF messages from daqsim
  - Simulate data storage/transfer operations
  - Send appropriate status updates to monitor
- [ ] Create `emulate_processing_agent` management command:
  - Listen for processing requests from data agent
  - Simulate data processing workflows
  - Report processing results and status
- [ ] Leverage existing ActiveMQ infrastructure (`activemq_listener.py`, `listen_activemq`)

### Phase 3: Monitoring Views

- [ ] Create workflow visualization dashboard:
  - Real-time STF processing pipeline status
  - Agent health and performance metrics
  - Historical workflow analysis
- [ ] Implement ActiveMQ traffic monitoring:
  - Message flow debugging interface
  - Error tracking and alerting
  - Performance bottleneck identification

### Phase 4: Message Protocol Design

- [ ] Define standardized message formats for each agent type
- [ ] Document message flow patterns and error handling
- [ ] Create protocol validation and testing framework
- [ ] Ensure seamless integration path for real agents

## Technical Configuration

### ActiveMQ Setup

**Local Development:**
- Set `MQ_LOCAL=1` in `~/.env` for no-SSL mode
- Default credentials: admin/admin on localhost:61616

**Production:**
- Uses SSL with certificate-based authentication
- Certificate management via environment variables

### STF Message Format

Example message from daqsim:
```json
{
    "filename": "swf.20250707.190903.run.physics.stf",
    "start": "20250707190900",
    "end": "20250707190903", 
    "state": "run",
    "substate": "physics",
    "msg_type": "stf_gen",
    "req_id": 1
}
```

### Development Environment

- Use swf-testbed virtual environment for agent emulation
- ActiveMQ and PostgreSQL via Docker Compose
- Monitor runs on Django development server (port 8002 recommended)

## Success Criteria

### Phase 1 Complete
- [ ] Complete STF workflow visible in monitor dashboard  
- [ ] Database schema supports full workflow tracking

### Phase 2 Complete  
- [ ] Emulated agents respond appropriately to daqsim messages
- [ ] Agent status updates flow correctly to monitor

### Phase 3 Complete
- [ ] Monitoring views provide actionable insights
- [ ] ActiveMQ traffic debugging available

### Phase 4 Complete
- [ ] Real agents can integrate without code changes
- [ ] Protocol documentation complete and validated

## Implementation Notes

### Agent State Management

Agents follow a state-based schedule:
- `no_beam` → `beam` → `run` states
- Various substates within each major state
- Status transitions trigger workflow actions

### Message Flow Patterns

1. **STF Generation**: daqsim → data-agent
2. **Data Processing**: data-agent → processing-agent  
3. **Analysis**: processing-agent → fastmon-agent
4. **Status Updates**: All agents → monitor

### Performance Considerations

- Design for high-throughput STF processing
- Efficient database indexing for workflow queries
- Scalable ActiveMQ topic management
- Real-time dashboard updates without blocking

## Related Documentation

- [MCP REST Implementation](MCP_REST_IMPLEMENTATION.md) - Protocol details
- [Setup Guide](SETUP_GUIDE.md) - Development environment
- [API Reference](API_REFERENCE.md) - Integration endpoints
- [Test Documentation](TEST_REFACTORING_REPORT.md) - Testing approach