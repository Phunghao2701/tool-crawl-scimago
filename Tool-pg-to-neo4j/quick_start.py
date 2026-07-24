#!/usr/bin/env python3
"""
Quick Start Guide Generator
Helps users run the setup tool and verify installation
"""

import subprocess
import sys
import os
from pathlib import Path

def print_header(text):
    """Print formatted header"""
    print("\n" + "="*70)
    print(f"  {text}")
    print("="*70 + "\n")

def print_step(num, text):
    """Print formatted step"""
    print(f"📍 Step {num}: {text}")

def print_success(text):
    """Print success message"""
    print(f"  ✅ {text}")

def print_error(text):
    """Print error message"""
    print(f"  ❌ {text}")

def print_info(text):
    """Print info message"""
    print(f"  ℹ️  {text}")

def check_python_version():
    """Check Python version"""
    print_step(1, "Checking Python version")
    version = sys.version_info
    if version.major >= 3 and version.minor >= 9:
        print_success(f"Python {version.major}.{version.minor}.{version.micro}")
        return True
    else:
        print_error(f"Python 3.9+ required, found {version.major}.{version.minor}")
        return False

def check_docker():
    """Check if Docker is installed"""
    print_step(2, "Checking Docker installation")
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print_success(result.stdout.strip())
            return True
        else:
            print_error("Docker found but not working properly")
            return False
    except FileNotFoundError:
        print_error("Docker not found in PATH")
        print_info("Install Docker Desktop from https://www.docker.com/products/docker-desktop")
        return False
    except Exception as e:
        print_error(f"Error checking Docker: {e}")
        return False

def check_docker_compose():
    """Check if Docker Compose is installed"""
    print_step(3, "Checking Docker Compose")
    try:
        result = subprocess.run(
            ["docker-compose", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print_success(result.stdout.strip())
            return True
        else:
            print_error("Docker Compose not working properly")
            return False
    except FileNotFoundError:
        print_error("Docker Compose not found in PATH")
        print_info("Install Docker Compose (usually included with Docker Desktop)")
        return False
    except Exception as e:
        print_error(f"Error checking Docker Compose: {e}")
        return False

def run_setup_tool(project_name="research-graph-sync", project_path=None):
    """Run the setup tool"""
    print_step(4, "Running setup tool")
    try:
        cmd = ["python", "setup_tool.py"]
        if project_path:
            cmd.extend(["--path", project_path])
        if project_name != "research-graph-sync":
            cmd.extend(["--name", project_name])
        
        result = subprocess.run(cmd, cwd=Path(__file__).parent)
        return result.returncode == 0
    except Exception as e:
        print_error(f"Error running setup tool: {e}")
        return False

def verify_setup():
    """Verify setup is complete"""
    print_step(5, "Verifying setup")
    
    required_dirs = [
        "research-graph-sync/src",
        "research-graph-sync/config",
        "research-graph-sync/docker",
        "research-graph-sync/database",
        "research-graph-sync/logs",
    ]
    
    required_files = [
        "research-graph-sync/.env",
        "research-graph-sync/README.md",
        "research-graph-sync/docker-compose.yml",
        "research-graph-sync/requirements.txt",
    ]
    
    all_ok = True
    
    for dir_path in required_dirs:
        if Path(dir_path).exists():
            print_success(f"Directory: {dir_path}")
        else:
            print_error(f"Missing directory: {dir_path}")
            all_ok = False
    
    for file_path in required_files:
        if Path(file_path).exists():
            print_success(f"File: {file_path}")
        else:
            print_error(f"Missing file: {file_path}")
            all_ok = False
    
    return all_ok

def main():
    """Main entry point"""
    print_header("RESEARCH GRAPH SYNC - QUICK START")
    
    print("This tool will help you set up the complete project structure,")
    print("environment configuration, and Docker setup.\n")
    
    # Check prerequisites
    print_header("CHECKING PREREQUISITES")
    
    checks = [
        ("Python", check_python_version),
        ("Docker", check_docker),
        ("Docker Compose", check_docker_compose),
    ]
    
    all_ok = True
    for name, check_func in checks:
        if not check_func():
            all_ok = False
    
    if not all_ok:
        print_header("SETUP CANNOT CONTINUE")
        print("Please install missing prerequisites and try again.")
        sys.exit(1)
    
    print_header("RUNNING SETUP")
    
    # Ask for custom names
    print("Enter project details (press Enter for defaults):\n")
    
    project_name = input("Project name (research-graph-sync): ").strip()
    if not project_name:
        project_name = "research-graph-sync"
    
    project_path = input("Project path (current directory): ").strip()
    if not project_path:
        project_path = None
    
    # Run setup
    if not run_setup_tool(project_name, project_path):
        print_error("Setup tool failed")
        sys.exit(1)
    
    print_header("VERIFYING SETUP")
    
    if not verify_setup():
        print_error("Some files are missing, but setup script completed")
        print_info("Check setup tool output for details")
    else:
        print_success("All required files created")
    
    print_header("NEXT STEPS")
    
    print(f"""
1. Navigate to project directory:
   cd {project_name}

2. Edit environment configuration:
   Edit .env file with your database credentials

3. Start services with Docker:
   docker-compose --env-file .env -f docker/docker-compose.yml up -d
   
   Or on Windows:
   docker-compose --env-file .env -f docker\\docker-compose.yml up -d

4. Wait for services to start (30-60 seconds):
   docker-compose --env-file .env -f docker/docker-compose.yml ps

5. Access services:
   - Neo4j Browser: http://localhost:7474
   - PostgreSQL: localhost:5432

6. Run initial sync:
   docker exec -it research_graph_sync python src/main.py --type full

7. Check logs:
   docker-compose logs -f app

For more information, see README.md and SETUP_GUIDE.md in the project.
""")
    
    print_header("SETUP COMPLETE")
    print("✅ Project is ready for configuration and deployment!")

if __name__ == "__main__":
    main()
