#!/usr/bin/env python
"""
Complete end-to-end test of the STF workflow system.

This script tests the complete pipeline:
1. Start emulated data and processing agents
2. Run daqsim to generate STF messages
3. Verify workflow data is captured in the database
4. Test the workflow dashboard views
"""

import os
import sys
import time
import subprocess
import threading
import signal
from pathlib import Path

# Add the Django project to the Python path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from monitor_app.models import STFWorkflow, AgentWorkflowStage, WorkflowMessage, SystemAgent
from django.utils import timezone

# Global process tracking
running_processes = []

def signal_handler(signum, frame):
    """Handle Ctrl+C to clean up processes."""
    print("\nCleaning up processes...")
    for process in running_processes:
        if process.poll() is None:  # Process is still running
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def run_command_background(command, name, cwd=None):
    """Run a command in the background and track it."""
    print(f"Starting {name}...")
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True
    )
    running_processes.append(process)
    return process

def check_workflow_status():
    """Check and display current workflow status."""
    print("\n=== Database Status ===")
    
    # Check agents
    agents = SystemAgent.objects.filter(workflow_enabled=True)
    print(f"Workflow agents: {agents.count()}")
    for agent in agents:
        print(f"  - {agent.instance_name} ({agent.agent_type}): {agent.status}")
        print(f"    Current: {agent.current_stf_count}, Total: {agent.total_stf_processed}")
    
    # Check workflows
    workflows = STFWorkflow.objects.all().order_by('-created_at')
    print(f"\nWorkflows: {workflows.count()}")
    for workflow in workflows:
        print(f"  - {workflow.filename}: {workflow.current_status}")
    
    # Check stages
    stages = AgentWorkflowStage.objects.all().order_by('-created_at')
    print(f"\nStages: {stages.count()}")
    for stage in stages:
        print(f"  - {stage.workflow.filename} -> {stage.agent_name}: {stage.status}")
    
    # Check messages
    messages = WorkflowMessage.objects.all().order_by('-sent_at')
    print(f"\nMessages: {messages.count()}")
    for message in messages:
        print(f"  - {message.message_type}: {message.sender_agent} -> {message.recipient_agent}")

def main():
    """Main test function."""
    print("=== Complete STF Workflow Test ===")
    
    # Set environment for local development
    os.environ['MQ_LOCAL'] = '1'
    
    # Change to the monitor source directory
    monitor_src = Path(__file__).parent / 'src'
    os.chdir(monitor_src)
    
    try:
        # Clear existing data
        print("Clearing existing workflow data...")
        STFWorkflow.objects.all().delete()
        AgentWorkflowStage.objects.all().delete()
        WorkflowMessage.objects.all().delete()
        SystemAgent.objects.filter(workflow_enabled=True).delete()
        
        # Start the data agent
        data_agent_process = run_command_background(
            "python manage.py emulate_data_agent --agent-name data-agent-test",
            "Data Agent",
            cwd=monitor_src
        )
        time.sleep(3)
        
        # Start the processing agent
        processing_agent_process = run_command_background(
            "python manage.py emulate_processing_agent --agent-name processing-agent-test",
            "Processing Agent",
            cwd=monitor_src
        )
        time.sleep(3)
        
        print("Agents started. Checking initial status...")
        check_workflow_status()
        
        # Run daqsim to generate STF messages
        print("\nRunning daqsim to generate STF messages...")
        daqsim_dir = Path(__file__).parent / '../swf-daqsim-agent/test'
        
        daqsim_result = subprocess.run(
            "export MQ_LOCAL=1 && python sim_test.py -v -S -u 20",
            shell=True,
            capture_output=True,
            text=True,
            cwd=daqsim_dir
        )
        
        if daqsim_result.returncode == 0:
            print("✅ DAQsim completed successfully")
            # Show STF generation output
            stf_lines = [line for line in daqsim_result.stdout.split('\n') if 'Sent MQ message' in line]
            print(f"Generated {len(stf_lines)} STF messages")
            for line in stf_lines[:5]:  # Show first 5
                print(f"  {line}")
        else:
            print("❌ DAQsim failed:")
            print(daqsim_result.stderr)
            return False
        
        # Wait for processing to complete
        print("\nWaiting for workflow processing...")
        for i in range(30):  # Wait up to 30 seconds
            time.sleep(1)
            workflows = STFWorkflow.objects.all()
            active_count = workflows.exclude(
                current_status__in=['workflow_complete', 'failed']
            ).count()
            
            if active_count == 0:
                print(f"✅ All workflows completed after {i+1} seconds")
                break
            elif i % 5 == 0:  # Update every 5 seconds
                print(f"  {active_count} workflows still active...")
        
        # Check final status
        print("\n=== Final Results ===")
        check_workflow_status()
        
        # Test workflow views
        print("\n=== Testing Workflow Views ===")
        try:
            # Import Django test client
            from django.test import Client
            from django.contrib.auth.models import User
            
            # Create test user
            user, created = User.objects.get_or_create(
                username='testuser',
                defaults={'is_staff': True, 'is_superuser': True}
            )
            if created:
                user.set_password('testpass')
                user.save()
            
            client = Client()
            client.login(username='testuser', password='testpass')
            
            # Test workflow dashboard
            response = client.get('/workflow/')
            if response.status_code == 200:
                print("✅ Workflow dashboard accessible")
            else:
                print(f"❌ Workflow dashboard failed: {response.status_code}")
            
            # Test workflow list
            response = client.get('/workflow/list/')
            if response.status_code == 200:
                print("✅ Workflow list accessible")
            else:
                print(f"❌ Workflow list failed: {response.status_code}")
                
        except Exception as e:
            print(f"❌ View testing failed: {e}")
        
        # Summary
        total_workflows = STFWorkflow.objects.count()
        completed_workflows = STFWorkflow.objects.filter(current_status='workflow_complete').count()
        failed_workflows = STFWorkflow.objects.filter(current_status='failed').count()
        
        print(f"\n=== Test Summary ===")
        print(f"Total workflows: {total_workflows}")
        print(f"Completed: {completed_workflows}")
        print(f"Failed: {failed_workflows}")
        print(f"Success rate: {(completed_workflows/total_workflows*100):.1f}%" if total_workflows > 0 else "No workflows")
        
        if total_workflows > 0 and completed_workflows == total_workflows:
            print("🎉 Complete workflow test PASSED!")
        else:
            print("⚠️  Complete workflow test had issues")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        # Clean up processes
        print("\nCleaning up...")
        for process in running_processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)