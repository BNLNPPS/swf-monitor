#!/usr/bin/env python
"""
Test script to verify the STF workflow with emulated agents.

This script demonstrates the complete workflow:
1. Start emulated data and processing agents
2. Run daqsim to generate STF messages
3. Verify workflow tracking in the database
"""

import os
import sys
import time
import subprocess
import threading
from pathlib import Path

# Add the Django project to the Python path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from monitor_app.models import STFWorkflow, AgentWorkflowStage, WorkflowMessage, SystemAgent


def run_command_in_thread(command, name):
    """Run a command in a separate thread."""
    def run():
        try:
            print(f"Starting {name}...")
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error in {name}: {result.stderr}")
            else:
                print(f"{name} output: {result.stdout}")
        except Exception as e:
            print(f"Exception in {name}: {e}")
    
    thread = threading.Thread(target=run, name=name)
    thread.daemon = True
    thread.start()
    return thread


def check_workflow_status():
    """Check the current status of workflows in the database."""
    print("\n=== Workflow Status ===")
    
    # Check agents
    agents = SystemAgent.objects.filter(workflow_enabled=True)
    print(f"Workflow agents: {agents.count()}")
    for agent in agents:
        print(f"  - {agent.instance_name} ({agent.agent_type}): {agent.status}")
        print(f"    Current STFs: {agent.current_stf_count}, Total: {agent.total_stf_processed}")
    
    # Check workflows
    workflows = STFWorkflow.objects.all().order_by('-created_at')[:10]
    print(f"\nRecent workflows: {workflows.count()}")
    for workflow in workflows:
        print(f"  - {workflow.filename}: {workflow.current_status} (agent: {workflow.current_agent})")
    
    # Check stages
    stages = AgentWorkflowStage.objects.all().order_by('-created_at')[:20]
    print(f"\nRecent stages: {stages.count()}")
    for stage in stages:
        print(f"  - {stage.workflow.filename} -> {stage.agent_name}: {stage.status}")
    
    # Check messages
    messages = WorkflowMessage.objects.all().order_by('-sent_at')[:10]
    print(f"\nRecent messages: {messages.count()}")
    for message in messages:
        print(f"  - {message.message_type}: {message.sender_agent} -> {message.recipient_agent}")


def main():
    """Main test function."""
    print("=== STF Workflow Test ===")
    
    # Set environment for local development
    os.environ['MQ_LOCAL'] = '1'
    
    # Change to the monitor source directory
    os.chdir(Path(__file__).parent / 'src')
    
    try:
        # Start the data agent
        print("Starting data agent...")
        data_agent_cmd = "python manage.py emulate_data_agent --agent-name data-agent-test"
        data_thread = run_command_in_thread(data_agent_cmd, "Data Agent")
        time.sleep(2)
        
        # Start the processing agent
        print("Starting processing agent...")
        processing_agent_cmd = "python manage.py emulate_processing_agent --agent-name processing-agent-test"
        processing_thread = run_command_in_thread(processing_agent_cmd, "Processing Agent")
        time.sleep(2)
        
        print("Agents started. Checking initial status...")
        check_workflow_status()
        
        # Run daqsim to generate STF messages
        print("\nRunning daqsim to generate STF messages...")
        daqsim_cmd = "cd ../../swf-daqsim-agent/test && export MQ_LOCAL=1 && python sim_test.py -v -S -u 15"
        daqsim_result = subprocess.run(daqsim_cmd, shell=True, capture_output=True, text=True)
        
        if daqsim_result.returncode == 0:
            print("DAQsim completed successfully")
            print("DAQsim output:", daqsim_result.stdout[-500:])  # Last 500 chars
        else:
            print("DAQsim failed:", daqsim_result.stderr)
            return
        
        # Wait for processing to complete
        print("\nWaiting for workflow processing to complete...")
        time.sleep(10)
        
        # Check final status
        print("\n=== Final Status ===")
        check_workflow_status()
        
    except KeyboardInterrupt:
        print("\nTest interrupted by user")
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()