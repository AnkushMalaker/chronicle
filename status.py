#!/usr/bin/env python3
"""
Chronicle Health Status Checker
Show runtime health status of all services
"""

import argparse
import subprocess
import sys
import json
import requests
from pathlib import Path
from typing import Dict, List, Any, Optional

from rich import print as rprint
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from dotenv import dotenv_values

# Import service definitions from services.py
from services import SERVICES, check_service_configured

console = Console()

# Health check endpoints
HEALTH_ENDPOINTS = {
    'backend': 'http://localhost:8000/health',
    'speaker-recognition': 'http://localhost:8085/health',
    'openmemory-mcp': 'http://localhost:8765/docs',  # No health endpoint, check docs
}


def get_container_status(service_name: str) -> Dict[str, Any]:
    """Get Docker container status for a service"""
    service = SERVICES[service_name]
    service_path = Path(service['path'])

    if not service_path.exists():
        return {'status': 'not_found', 'containers': []}

    try:
        # Get container status using docker compose ps
        cmd = ['docker', 'compose', 'ps', '--format', 'json']

        # Handle special profiles for backend (HTTPS and Obsidian)
        if service_name == 'backend':
            profiles = []
            
            # Check for HTTPS profile
            caddyfile_path = service_path / 'Caddyfile'
            if caddyfile_path.exists():
                profiles.append('https')
            
            # Check for Obsidian/Neo4j profile
            env_file = service_path / '.env'
            if env_file.exists():
                env_values = dotenv_values(env_file)
                neo4j_host = env_values.get('NEO4J_HOST', '')
                if neo4j_host and neo4j_host not in ['', 'your-neo4j-host-here', 'your_neo4j_host_here']:
                    profiles.append('obsidian')
            
            # Apply profiles if any are needed
            if profiles:
                cmd = ['docker', 'compose'] + [item for profile in profiles for item in ['--profile', profile]] + ['ps', '--format', 'json']

        # Handle speaker-recognition profiles
        if service_name == 'speaker-recognition':
            from dotenv import dotenv_values
            env_file = service_path / '.env'
            if env_file.exists():
                env_values = dotenv_values(env_file)
                compute_mode = env_values.get('COMPUTE_MODE', 'cpu')
                if compute_mode == 'gpu':
                    cmd = ['docker', 'compose', '--profile', 'gpu', 'ps', '--format', 'json']
                else:
                    cmd = ['docker', 'compose', '--profile', 'cpu', 'ps', '--format', 'json']

        result = subprocess.run(
            cmd,
            cwd=service_path,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return {'status': 'error', 'containers': [], 'error': result.stderr}

        # Parse JSON output (one JSON object per line)
        containers = []
        for line in result.stdout.strip().split('\n'):
            if line:
                try:
                    container = json.loads(line)
                    containers.append({
                        'name': container.get('Name', 'unknown'),
                        'state': container.get('State', 'unknown'),
                        'status': container.get('Status', 'unknown'),
                        'health': container.get('Health', 'none')
                    })
                except json.JSONDecodeError:
                    continue

        if not containers:
            return {'status': 'stopped', 'containers': []}

        # Determine overall status
        all_running = all(c['state'] == 'running' for c in containers)
        any_running = any(c['state'] == 'running' for c in containers)

        if all_running:
            status = 'running'
        elif any_running:
            status = 'partial'
        else:
            status = 'stopped'

        return {'status': status, 'containers': containers}

    except subprocess.TimeoutExpired:
        return {'status': 'timeout', 'containers': []}
    except Exception as e:
        return {'status': 'error', 'containers': [], 'error': str(e)}


def check_http_health(url: str, timeout: int = 5) -> Dict[str, Any]:
    """Check HTTP health endpoint"""
    try:
        response = requests.get(url, timeout=timeout)

        if response.status_code == 200:
            # Try to parse JSON response
            try:
                data = response.json()
                return {'healthy': True, 'status_code': 200, 'data': data}
            except json.JSONDecodeError:
                return {'healthy': True, 'status_code': 200, 'data': None}
        else:
            return {'healthy': False, 'status_code': response.status_code, 'data': None}

    except requests.exceptions.ConnectionError:
        return {'healthy': False, 'error': 'Connection refused'}
    except requests.exceptions.Timeout:
        return {'healthy': False, 'error': 'Timeout'}
    except Exception as e:
        return {'healthy': False, 'error': str(e)}


def get_service_health(service_name: str) -> Dict[str, Any]:
    """Get comprehensive health status for a service"""
    # Check if configured
    if not check_service_configured(service_name):
        return {
            'configured': False,
            'container_status': 'not_configured',
            'health': None
        }

    # Get container status
    container_info = get_container_status(service_name)

    # Check HTTP health endpoint if available
    health_check = None
    if service_name in HEALTH_ENDPOINTS:
        url = HEALTH_ENDPOINTS[service_name]
        health_check = check_http_health(url)

    return {
        'configured': True,
        'container_status': container_info['status'],
        'containers': container_info.get('containers', []),
        'health': health_check
    }


def show_quick_status():
    """Show quick status overview"""
    console.print("\n🏥 [bold]Chronicle Health Status[/bold]\n")

    table = Table(title="Service Status Overview")
    table.add_column("Service", style="cyan", no_wrap=True)
    table.add_column("Config", justify="center")
    table.add_column("Containers", justify="center")
    table.add_column("Health", justify="center")
    table.add_column("Description", style="dim")

    for service_name, service_info in SERVICES.items():
        status = get_service_health(service_name)

        # Config status
        config_icon = "✅" if status['configured'] else "❌"

        # Container status
        if not status['configured']:
            container_icon = "⚪"
        elif status['container_status'] == 'running':
            container_icon = "🟢"
        elif status['container_status'] == 'partial':
            container_icon = "🟡"
        elif status['container_status'] == 'stopped':
            container_icon = "🔴"
        else:
            container_icon = "⚫"

        # Health status
        if status['health'] is None:
            health_icon = "⚪"
        elif status['health'].get('healthy'):
            health_icon = "✅"
        else:
            health_icon = "❌"

        table.add_row(
            service_name,
            config_icon,
            container_icon,
            health_icon,
            service_info['description']
        )

    console.print(table)

    # Legend
    console.print("\n[dim]Legend:[/dim]")
    console.print("[dim]  Containers: 🟢 Running | 🟡 Partial | 🔴 Stopped | ⚪ Not Configured | ⚫ Error[/dim]")
    console.print("[dim]  Health: ✅ Healthy | ❌ Unhealthy | ⚪ No Endpoint[/dim]")


def show_detailed_status():
    """Show detailed status with backend health breakdown"""
    console.print("\n🏥 [bold]Chronicle Detailed Health Status[/bold]\n")

    # Get all service statuses
    for service_name, service_info in SERVICES.items():
        status = get_service_health(service_name)

        # Service header
        if status['configured']:
            header = f"📦 {service_name.upper()}"
        else:
            header = f"📦 {service_name.upper()} (Not Configured)"

        console.print(f"\n[bold cyan]{header}[/bold cyan]")
        console.print(f"[dim]{service_info['description']}[/dim]")

        if not status['configured']:
            console.print("[yellow]  ⚠️  Not configured (no .env file)[/yellow]")
            continue

        # Container status
        console.print(f"\n  [bold]Containers:[/bold]")
        if status['container_status'] == 'running':
            console.print(f"    [green]🟢 All containers running[/green]")
        elif status['container_status'] == 'partial':
            console.print(f"    [yellow]🟡 Some containers running[/yellow]")
        elif status['container_status'] == 'stopped':
            console.print(f"    [red]🔴 All containers stopped[/red]")
        else:
            console.print(f"    [red]⚫ Error checking containers[/red]")

        # Show container details
        for container in status.get('containers', []):
            state_icon = "🟢" if container['state'] == 'running' else "🔴"
            health_status = f" ({container['health']})" if container['health'] != 'none' else ""
            console.print(f"      {state_icon} {container['name']}: {container['status']}{health_status}")

        # HTTP Health check
        if status['health'] is not None:
            console.print(f"\n  [bold]HTTP Health:[/bold]")

            if status['health'].get('healthy'):
                console.print(f"    [green]✅ Healthy[/green]")

                # For backend, show detailed health data
                if service_name == 'backend' and status['health'].get('data'):
                    health_data = status['health']['data']

                    # Overall status
                    overall_status = health_data.get('status', 'unknown')
                    if overall_status == 'healthy':
                        console.print(f"      Overall: [green]{overall_status}[/green]")
                    elif overall_status == 'degraded':
                        console.print(f"      Overall: [yellow]{overall_status}[/yellow]")
                    else:
                        console.print(f"      Overall: [red]{overall_status}[/red]")

                    # Critical services
                    services = health_data.get('services', {})
                    console.print(f"\n      [bold]Critical Services:[/bold]")

                    for svc_name in ['mongodb', 'redis']:
                        if svc_name in services:
                            svc = services[svc_name]
                            if svc.get('healthy'):
                                console.print(f"        [green]✅ {svc_name}: {svc.get('status', 'ok')}[/green]")
                            else:
                                console.print(f"        [red]❌ {svc_name}: {svc.get('status', 'error')}[/red]")

                    # Optional services
                    console.print(f"\n      [bold]Optional Services:[/bold]")
                    optional_services = ['audioai', 'memory_service', 'speech_to_text', 'speaker_recognition', 'openmemory_mcp']
                    for svc_name in optional_services:
                        if svc_name in services:
                            svc = services[svc_name]
                            if svc.get('healthy'):
                                console.print(f"        [green]✅ {svc_name}: {svc.get('status', 'ok')}[/green]")
                            else:
                                console.print(f"        [yellow]⚠️  {svc_name}: {svc.get('status', 'degraded')}[/yellow]")

                    # Configuration info
                    config = health_data.get('config', {})
                    if config:
                        console.print(f"\n      [bold]Configuration:[/bold]")
                        console.print(f"        LLM: {config.get('llm_provider', 'unknown')} ({config.get('llm_model', 'unknown')})")
                        console.print(f"        Transcription: {config.get('transcription_service', 'unknown')}")
                        console.print(f"        Active Clients: {config.get('active_clients', 0)}")
            else:
                error = status['health'].get('error', 'Unknown error')
                console.print(f"    [red]❌ Unhealthy: {error}[/red]")

        console.print("")  # Spacing


def show_json_status():
    """Show status in JSON format for programmatic consumption"""
    status_data = {}

    for service_name in SERVICES.keys():
        status_data[service_name] = get_service_health(service_name)

    print(json.dumps(status_data, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Chronicle Health Status Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./status.sh              Show quick status overview
  ./status.sh --detailed   Show detailed health information
  ./status.sh --json       Output status in JSON format
        """
    )

    parser.add_argument(
        '--detailed', '-d',
        action='store_true',
        help='Show detailed health information including backend service breakdown'
    )

    parser.add_argument(
        '--json', '-j',
        action='store_true',
        help='Output status in JSON format'
    )

    args = parser.parse_args()

    if args.json:
        show_json_status()
    elif args.detailed:
        show_detailed_status()
    else:
        show_quick_status()

    console.print("\n💡 [dim]Tip: Use './status.sh --detailed' for comprehensive health checks[/dim]\n")


if __name__ == "__main__":
    main()
