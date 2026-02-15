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


def get_restart_counts(container_names: List[str]) -> Dict[str, int]:
    """Get restart counts for containers via docker inspect"""
    if not container_names:
        return {}
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{.Name}} {{.RestartCount}}'] + container_names,
            capture_output=True,
            text=True,
            timeout=10
        )
        counts = {}
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                parts = line.strip().rsplit(' ', 1)
                if len(parts) == 2:
                    name = parts[0].lstrip('/')
                    try:
                        counts[name] = int(parts[1])
                    except ValueError:
                        counts[name] = 0
        return counts
    except Exception:
        return {}


def get_container_status(service_name: str) -> Dict[str, Any]:
    """Get Docker container status for a service"""
    service = SERVICES[service_name]
    service_path = Path(service['path'])

    if not service_path.exists():
        return {'status': 'not_found', 'containers': []}

    try:
        # Get container status using docker compose ps
        # Only check containers from active profiles (excludes inactive profile services)
        cmd = ['docker', 'compose', 'ps', '--format', 'json']

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
                    container_name = container.get('Name', 'unknown')

                    # Skip test containers - they're not part of production services
                    if '-test-' in container_name.lower():
                        continue

                    containers.append({
                        'name': container_name,
                        'state': container.get('State', 'unknown'),
                        'status': container.get('Status', 'unknown'),
                        'health': container.get('Health', 'none')
                    })
                except json.JSONDecodeError:
                    continue

        if not containers:
            return {'status': 'stopped', 'containers': []}

        # Fetch restart counts via docker inspect
        container_names = [c['name'] for c in containers]
        restart_counts = get_restart_counts(container_names)
        for container in containers:
            container['restart_count'] = restart_counts.get(container['name'], 0)

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


def get_backend_worker_health() -> Optional[Dict[str, Any]]:
    """Get internal worker health from the backend /health endpoint.

    Returns worker_count, failed queues, etc. from the Redis section of health data.
    This catches internal worker crash loops that Docker restart counts miss.
    """
    try:
        response = requests.get('http://localhost:8000/health', timeout=5)
        if response.status_code == 200:
            data = response.json()
            redis_info = data.get('services', {}).get('redis', {})
            return {
                'worker_count': redis_info.get('worker_count', 0),
                'active_workers': redis_info.get('active_workers', 0),
                'idle_workers': redis_info.get('idle_workers', 0),
                'queues': redis_info.get('queues', {}),
            }
    except Exception:
        pass
    return None


def show_quick_status():
    """Show quick status overview"""
    console.print("\n🏥 [bold]Chronicle Health Status[/bold]\n")

    table = Table(title="Service Status Overview")
    table.add_column("Service", style="cyan", no_wrap=True)
    table.add_column("Config", justify="center")
    table.add_column("Containers", justify="center")
    table.add_column("Restarts", justify="center")
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
        elif status['container_status'] == 'not_found':
            container_icon = "⚪"
        elif status['container_status'] in ['error', 'timeout']:
            container_icon = "⚫"
        else:
            # Unknown status - log it for debugging
            container_icon = "⚫"

        # Restart count
        total_restarts = sum(c.get('restart_count', 0) for c in status.get('containers', []))
        if not status['configured'] or not status.get('containers'):
            restart_text = "⚪"
        elif total_restarts > 0:
            restart_text = f"[bold red]⚠️  {total_restarts}[/bold red]"
        else:
            restart_text = "[green]0[/green]"

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
            restart_text,
            health_icon,
            service_info['description']
        )

    console.print(table)

    # Worker health note (from backend /health endpoint)
    worker_health = get_backend_worker_health()
    if worker_health is not None:
        wc = worker_health['worker_count']
        active = worker_health['active_workers']
        total_failed = sum(q.get('failed_count', 0) for q in worker_health['queues'].values())
        if wc == 0:
            console.print("\n[bold red]  ❌ RQ Workers: 0 registered — workers may be crash-looping. Check: docker compose logs workers[/bold red]")
        elif total_failed > 0:
            console.print(f"\n  [yellow]⚠️  RQ Workers: {wc} registered ({active} active), {total_failed} failed job(s) in queues[/yellow]")
        else:
            console.print(f"\n  [green]✅ RQ Workers: {wc} registered ({active} active)[/green]")

    # Legend
    console.print("\n[dim]Legend:[/dim]")
    console.print("[dim]  Containers: 🟢 Running | 🟡 Partial | 🔴 Stopped | ⚪ Not Configured | ⚫ Error[/dim]")
    console.print("[dim]  Restarts: 0 = stable | ⚠️  N = container crashed N times (restart loop)[/dim]")
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
            restart_count = container.get('restart_count', 0)
            restart_info = f" [bold red]⚠️  {restart_count} restarts[/bold red]" if restart_count > 0 else ""
            console.print(f"      {state_icon} {container['name']}: {container['status']}{health_status}{restart_info}")

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
