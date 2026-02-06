#!/usr/bin/env python3
"""
Chronicle Service Management
Start, stop, and manage configured services
"""

import argparse
import subprocess
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table
from dotenv import dotenv_values

console = Console()

def load_config_yml():
    """Load config.yml from repository root"""
    config_path = Path(__file__).parent / 'config' / 'config.yml'
    if not config_path.exists():
        return None

    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        console.print(f"[yellow]⚠️  Warning: Could not load config/config.yml: {e}[/yellow]")
        return None

SERVICES = {
    'backend': {
        'path': 'backends/advanced',
        'compose_file': 'docker-compose.yml',
        'description': 'Advanced Backend + WebUI',
        'ports': ['8000', '5173']
    },
    'speaker-recognition': {
        'path': 'extras/speaker-recognition', 
        'compose_file': 'docker-compose.yml',
        'description': 'Speaker Recognition Service',
        'ports': ['8085', '5174/8444']
    },
    'asr-services': {
        'path': 'extras/asr-services',
        'compose_file': 'docker-compose.yml', 
        'description': 'Parakeet ASR Service',
        'ports': ['8767']
    },
    'openmemory-mcp': {
        'path': 'extras/openmemory-mcp',
        'compose_file': 'docker-compose.yml',
        'description': 'OpenMemory MCP Server', 
        'ports': ['8765']
    }
}

def check_service_configured(service_name):
    """Check if service is configured (has .env file)"""
    service = SERVICES[service_name]
    service_path = Path(service['path'])
    
    # Backend uses advanced init, others use .env
    if service_name == 'backend':
        return (service_path / '.env').exists()
    else:
        return (service_path / '.env').exists()

def run_compose_command(service_name, command, build=False):
    """Run docker compose command for a service"""
    service = SERVICES[service_name]
    service_path = Path(service['path'])

    if not service_path.exists():
        console.print(f"[red]❌ Service directory not found: {service_path}[/red]")
        return False

    compose_file = service_path / service['compose_file']
    if not compose_file.exists():
        console.print(f"[red]❌ Docker compose file not found: {compose_file}[/red]")
        return False

    # Step 1: If build is requested, run build separately first (no timeout for CUDA builds)
    if build and command == 'up':
        # Build command - need to specify profiles for build too
        build_cmd = ['docker', 'compose']

        # Add profiles to build command (needed for profile-specific services)
        if service_name == 'backend':
            caddyfile_path = service_path / 'Caddyfile'
            if caddyfile_path.exists() and caddyfile_path.is_file():
                build_cmd.extend(['--profile', 'https'])

            obsidian_enabled = False
            kg_enabled = False
            config_data = load_config_yml()
            if config_data:
                memory_config = config_data.get('memory', {})
                obsidian_config = memory_config.get('obsidian', {})
                if obsidian_config.get('enabled', False):
                    obsidian_enabled = True
                kg_config = memory_config.get('knowledge_graph', {})
                if kg_config.get('enabled', False):
                    kg_enabled = True

            if not obsidian_enabled:
                env_file = service_path / '.env'
                if env_file.exists():
                    env_values = dotenv_values(env_file)
                    if env_values.get('OBSIDIAN_ENABLED', 'false').lower() == 'true':
                        obsidian_enabled = True

            if obsidian_enabled:
                build_cmd.extend(['--profile', 'obsidian'])
            if kg_enabled:
                build_cmd.extend(['--profile', 'knowledge-graph'])

        elif service_name == 'speaker-recognition':
            env_file = service_path / '.env'
            if env_file.exists():
                env_values = dotenv_values(env_file)
                # Derive profile from PYTORCH_CUDA_VERSION (cu126/cu121/etc = gpu, cpu = cpu)
                pytorch_version = env_values.get('PYTORCH_CUDA_VERSION', 'cpu')
                profile = 'gpu' if pytorch_version.startswith('cu') else 'cpu'
                build_cmd.extend(['--profile', profile])

        # For asr-services, only build the selected provider
        asr_service_to_build = None
        if service_name == 'asr-services':
            env_file = service_path / '.env'
            if env_file.exists():
                env_values = dotenv_values(env_file)
                asr_provider = env_values.get('ASR_PROVIDER', '').strip("'\"")

                # Map provider to docker service name
                provider_to_service = {
                    'vibevoice': 'vibevoice-asr',
                    'faster-whisper': 'faster-whisper-asr',
                    'transformers': 'transformers-asr',
                    'nemo': 'nemo-asr',
                    'parakeet': 'parakeet-asr',
                }
                asr_service_to_build = provider_to_service.get(asr_provider)

                if asr_service_to_build:
                    console.print(f"[blue]ℹ️  Building ASR provider: {asr_provider} ({asr_service_to_build})[/blue]")

        build_cmd.append('build')

        # If building ASR, only build the specific service
        if asr_service_to_build:
            build_cmd.append(asr_service_to_build)

        # Run build with streaming output (no timeout)
        console.print(f"[cyan]🔨 Building {service_name} (this may take several minutes for CUDA/GPU builds)...[/cyan]")
        try:
            process = subprocess.Popen(
                build_cmd,
                cwd=service_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            if process.stdout is None:
                raise RuntimeError("Process stdout is None - unable to read command output")

            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                if 'error' in line.lower() or 'failed' in line.lower():
                    console.print(f"  [red]{line}[/red]")
                elif 'Successfully' in line or 'built' in line.lower():
                    console.print(f"  [green]{line}[/green]")
                elif 'Building' in line or 'Step' in line:
                    console.print(f"  [cyan]{line}[/cyan]")
                elif 'warning' in line.lower():
                    console.print(f"  [yellow]{line}[/yellow]")
                else:
                    console.print(f"  [dim]{line}[/dim]")

            process.wait()

            if process.returncode != 0:
                console.print(f"\n[red]❌ Build failed for {service_name}[/red]")
                return False

            console.print(f"[green]✅ Build completed for {service_name}[/green]")

        except Exception as e:
            console.print(f"[red]❌ Error building {service_name}: {e}[/red]")
            return False

    # Step 2: Run the actual command (up/down/restart/status)
    cmd = ['docker', 'compose']

    # Add profiles for backend service
    if service_name == 'backend':
        caddyfile_path = service_path / 'Caddyfile'
        if caddyfile_path.exists() and caddyfile_path.is_file():
            cmd.extend(['--profile', 'https'])

        obsidian_enabled = False
        kg_enabled = False
        config_data = load_config_yml()
        if config_data:
            memory_config = config_data.get('memory', {})
            obsidian_config = memory_config.get('obsidian', {})
            if obsidian_config.get('enabled', False):
                obsidian_enabled = True
            kg_config = memory_config.get('knowledge_graph', {})
            if kg_config.get('enabled', False):
                kg_enabled = True

        if not obsidian_enabled:
            env_file = service_path / '.env'
            if env_file.exists():
                env_values = dotenv_values(env_file)
                if env_values.get('OBSIDIAN_ENABLED', 'false').lower() == 'true':
                    obsidian_enabled = True

        if obsidian_enabled:
            cmd.extend(['--profile', 'obsidian'])
            console.print("[blue]ℹ️  Starting with Obsidian/Neo4j support[/blue]")
        if kg_enabled:
            cmd.extend(['--profile', 'knowledge-graph'])
            console.print("[blue]ℹ️  Starting with Knowledge Graph (Neo4j)[/blue]")

    # Handle speaker-recognition service specially
    if service_name == 'speaker-recognition' and command in ['up', 'down']:
        env_file = service_path / '.env'
        if env_file.exists():
            env_values = dotenv_values(env_file)
            # Derive profile from PYTORCH_CUDA_VERSION (cu126/cu121/etc = gpu, cpu = cpu)
            pytorch_version = env_values.get('PYTORCH_CUDA_VERSION', 'cpu')
            profile = 'gpu' if pytorch_version.startswith('cu') else 'cpu'

            cmd.extend(['--profile', profile])

            if command == 'up':
                https_enabled = env_values.get('REACT_UI_HTTPS', 'false')
                if https_enabled.lower() == 'true':
                    cmd.extend(['up', '-d'])
                else:
                    cmd.extend(['up', '-d', 'speaker-service-gpu' if profile == 'gpu' else 'speaker-service-cpu', 'web-ui'])
            elif command == 'down':
                cmd.extend(['down'])
        else:
            if command == 'up':
                cmd.extend(['up', '-d'])
            elif command == 'down':
                cmd.extend(['down'])

    # Handle asr-services - start only the configured provider
    elif service_name == 'asr-services' and command in ['up', 'down', 'restart']:
        env_file = service_path / '.env'
        asr_service_name = None

        if env_file.exists():
            env_values = dotenv_values(env_file)
            asr_provider = env_values.get('ASR_PROVIDER', '').strip("'\"")

            # Map provider to docker service name
            provider_to_service = {
                'vibevoice': 'vibevoice-asr',
                'faster-whisper': 'faster-whisper-asr',
                'transformers': 'transformers-asr',
                'nemo': 'nemo-asr',
                'parakeet': 'parakeet-asr',
            }
            asr_service_name = provider_to_service.get(asr_provider)

            if asr_service_name:
                console.print(f"[blue]ℹ️  Using ASR provider: {asr_provider} ({asr_service_name})[/blue]")

        if command == 'up':
            if asr_service_name:
                cmd.extend(['up', '-d', asr_service_name])
            else:
                console.print("[yellow]⚠️  No ASR_PROVIDER configured, starting default service[/yellow]")
                cmd.extend(['up', '-d', 'vibevoice-asr'])
        elif command == 'down':
            cmd.extend(['down'])
        elif command == 'restart':
            if asr_service_name:
                cmd.extend(['restart', asr_service_name])
            else:
                cmd.extend(['restart'])

    else:
        # Standard compose commands for other services
        if command == 'up':
            cmd.extend(['up', '-d'])
        elif command == 'down':
            cmd.extend(['down'])
        elif command == 'restart':
            cmd.extend(['restart'])
        elif command == 'status':
            cmd.extend(['ps'])

    try:
        # Run the command with timeout (build already done if needed)
        result = subprocess.run(
            cmd,
            cwd=service_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=120  # 2 minute timeout
        )

        if result.returncode == 0:
            return True
        else:
            console.print(f"[red]❌ Command failed[/red]")
            if result.stderr:
                console.print("[red]Error output:[/red]")
                for line in result.stderr.splitlines():
                    console.print(f"  [dim]{line}[/dim]")
            return False

    except subprocess.TimeoutExpired:
        console.print(f"[red]❌ Command timed out after 2 minutes for {service_name}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]❌ Error running command: {e}[/red]")
        return False

def ensure_docker_network():
    """Ensure chronicle-network exists"""
    try:
        # Check if network already exists
        result = subprocess.run(
            ['docker', 'network', 'inspect', 'chronicle-network'],
            capture_output=True,
            check=False
        )

        if result.returncode != 0:
            # Network doesn't exist, create it
            console.print("[blue]📡 Creating chronicle-network...[/blue]")
            subprocess.run(
                ['docker', 'network', 'create', 'chronicle-network'],
                check=True,
                capture_output=True
            )
            console.print("[green]✅ chronicle-network created[/green]")
        else:
            console.print("[dim]📡 chronicle-network already exists[/dim]")
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Failed to create network: {e}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]❌ Error checking/creating network: {e}[/red]")
        return False

def start_services(services, build=False):
    """Start specified services"""
    console.print(f"🚀 [bold]Starting {len(services)} services...[/bold]")

    # Ensure Docker network exists before starting services
    if not ensure_docker_network():
        console.print("[red]❌ Cannot start services without Docker network[/red]")
        return

    success_count = 0
    for service_name in services:
        if service_name not in SERVICES:
            console.print(f"[red]❌ Unknown service: {service_name}[/red]")
            continue
            
        if not check_service_configured(service_name):
            console.print(f"[yellow]⚠️  {service_name} not configured, skipping[/yellow]")
            continue
            
        console.print(f"\n🔧 Starting {service_name}...")
        if run_compose_command(service_name, 'up', build):
            console.print(f"[green]✅ {service_name} started[/green]")
            success_count += 1
        else:
            console.print(f"[red]❌ Failed to start {service_name}[/red]")
    
    console.print(f"\n[green]🎉 {success_count}/{len(services)} services started successfully[/green]")

def stop_services(services):
    """Stop specified services"""
    console.print(f"🛑 [bold]Stopping {len(services)} services...[/bold]")

    success_count = 0
    for service_name in services:
        if service_name not in SERVICES:
            console.print(f"[red]❌ Unknown service: {service_name}[/red]")
            continue

        console.print(f"\n🔧 Stopping {service_name}...")
        if run_compose_command(service_name, 'down'):
            console.print(f"[green]✅ {service_name} stopped[/green]")
            success_count += 1
        else:
            console.print(f"[red]❌ Failed to stop {service_name}[/red]")

    console.print(f"\n[green]🎉 {success_count}/{len(services)} services stopped successfully[/green]")

def restart_services(services, recreate=False):
    """Restart specified services"""
    console.print(f"🔄 [bold]Restarting {len(services)} services...[/bold]")

    if recreate:
        console.print("[dim]Using down + up to recreate containers (fixes WSL2 bind mount issues)[/dim]\n")
    else:
        console.print("[dim]Quick restart (use --recreate to fix bind mount issues)[/dim]\n")

    success_count = 0
    for service_name in services:
        if service_name not in SERVICES:
            console.print(f"[red]❌ Unknown service: {service_name}[/red]")
            continue

        if not check_service_configured(service_name):
            console.print(f"[yellow]⚠️  {service_name} not configured, skipping[/yellow]")
            continue

        console.print(f"\n🔧 Restarting {service_name}...")

        if recreate:
            # Full recreation: down + up (fixes bind mount issues)
            if not run_compose_command(service_name, 'down'):
                console.print(f"[red]❌ Failed to stop {service_name}[/red]")
                continue

            if run_compose_command(service_name, 'up'):
                console.print(f"[green]✅ {service_name} restarted[/green]")
                success_count += 1
            else:
                console.print(f"[red]❌ Failed to start {service_name}[/red]")
        else:
            # Quick restart: docker compose restart
            if run_compose_command(service_name, 'restart'):
                console.print(f"[green]✅ {service_name} restarted[/green]")
                success_count += 1
            else:
                console.print(f"[red]❌ Failed to restart {service_name}[/red]")

    console.print(f"\n[green]🎉 {success_count}/{len(services)} services restarted successfully[/green]")

def show_status():
    """Show status of all services"""
    console.print("📊 [bold]Service Status:[/bold]\n")
    
    table = Table()
    table.add_column("Service", style="cyan")
    table.add_column("Configured", justify="center")
    table.add_column("Description", style="dim")
    table.add_column("Ports", style="green")
    
    for service_name, service_info in SERVICES.items():
        configured = "✅" if check_service_configured(service_name) else "❌"
        ports = ", ".join(service_info['ports'])
        table.add_row(
            service_name,
            configured, 
            service_info['description'],
            ports
        )
    
    console.print(table)
    
    console.print("\n💡 [dim]Use 'python services.py start --all' to start all configured services[/dim]")

def main():
    parser = argparse.ArgumentParser(description="Chronicle Service Management")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Start command
    start_parser = subparsers.add_parser('start', help='Start services')
    start_parser.add_argument('services', nargs='*', 
                            help='Services to start: backend, speaker-recognition, asr-services, openmemory-mcp (or use --all)')
    start_parser.add_argument('--all', action='store_true', help='Start all configured services')
    start_parser.add_argument('--build', action='store_true', help='Build images before starting')
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop services')
    stop_parser.add_argument('services', nargs='*',
                           help='Services to stop: backend, speaker-recognition, asr-services, openmemory-mcp (or use --all)')
    stop_parser.add_argument('--all', action='store_true', help='Stop all services')

    # Restart command
    restart_parser = subparsers.add_parser('restart', help='Restart services')
    restart_parser.add_argument('services', nargs='*',
                               help='Services to restart: backend, speaker-recognition, asr-services, openmemory-mcp (or use --all)')
    restart_parser.add_argument('--all', action='store_true', help='Restart all services')
    restart_parser.add_argument('--recreate', action='store_true',
                               help='Recreate containers (down + up) instead of quick restart - fixes WSL2 bind mount issues')

    # Status command
    subparsers.add_parser('status', help='Show service status')
    
    args = parser.parse_args()
    
    if not args.command:
        show_status()
        return
    
    if args.command == 'status':
        show_status()
        
    elif args.command == 'start':
        if args.all:
            services = [s for s in SERVICES.keys() if check_service_configured(s)]
        elif args.services:
            # Validate service names
            invalid_services = [s for s in args.services if s not in SERVICES]
            if invalid_services:
                console.print(f"[red]❌ Invalid service names: {', '.join(invalid_services)}[/red]")
                console.print(f"Available services: {', '.join(SERVICES.keys())}")
                return
            services = args.services
        else:
            console.print("[red]❌ No services specified. Use --all or specify service names.[/red]")
            return
            
        start_services(services, args.build)
        
    elif args.command == 'stop':
        if args.all:
            # Only stop configured services (like start --all does)
            services = [s for s in SERVICES.keys() if check_service_configured(s)]
        elif args.services:
            # Validate service names
            invalid_services = [s for s in args.services if s not in SERVICES]
            if invalid_services:
                console.print(f"[red]❌ Invalid service names: {', '.join(invalid_services)}[/red]")
                console.print(f"Available services: {', '.join(SERVICES.keys())}")
                return
            services = args.services
        else:
            console.print("[red]❌ No services specified. Use --all or specify service names.[/red]")
            return

        stop_services(services)

    elif args.command == 'restart':
        if args.all:
            services = [s for s in SERVICES.keys() if check_service_configured(s)]
        elif args.services:
            # Validate service names
            invalid_services = [s for s in args.services if s not in SERVICES]
            if invalid_services:
                console.print(f"[red]❌ Invalid service names: {', '.join(invalid_services)}[/red]")
                console.print(f"Available services: {', '.join(SERVICES.keys())}")
                return
            services = args.services
        else:
            console.print("[red]❌ No services specified. Use --all or specify service names.[/red]")
            return

        restart_services(services, recreate=args.recreate)

if __name__ == "__main__":
    main()