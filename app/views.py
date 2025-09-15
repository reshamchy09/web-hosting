from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import WebsiteForm, SignupForm, DjangoProjectForm
from .models import Website, DjangoProject
from .utils import (
    deploy_django_project, 
    check_django_deployment_status,
    cleanup_django_deployment,
    get_django_project_info
)
from django.conf import settings
from django.http import JsonResponse
import os
import uuid
import logging

logger = logging.getLogger(__name__)

# Existing public pages
def home(request):
    return render(request, 'home.html')

def about(request):
    return render(request, 'about.html')

def plans(request):
    return render(request, 'plans.html')

def contact(request):
    return render(request, 'contact.html')

# Auth
def signup_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')

        if password != confirm_password:
            messages.error(request, "Passwords do not match!")
            return redirect('signup')

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already taken!")
            return redirect('signup')

        user = User.objects.create_user(username=username, email=email, password=password)
        user.save()
        messages.success(request, "Signup successful! Please log in.")
        return redirect('login')

    return render(request, 'signup.html')

def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect('dashboard')
        else:
            messages.error(request, "Invalid credentials")
            return redirect('login')
    return render(request, 'login.html')

def logout_view(request):
    logout(request)
    return redirect('home')

# Dashboard
@login_required
def dashboard_view(request):
    # Get both static websites and Django projects
    websites = Website.objects.filter(user=request.user)
    django_projects = DjangoProject.objects.filter(user=request.user)
    
    context = {
        'websites': websites,
        'django_projects': django_projects,
        'total_sites': websites.count() + django_projects.count(),
        'active_sites': websites.filter(is_active=True).count() + django_projects.filter(is_active=True).count()
    }
    
    return render(request, 'dashboard.html', context)

# Django Project Management

@login_required
def deploy_django_view(request):
    """Deploy Django project with improved error handling"""
    if request.method == 'POST':
        logger.info(f"POST data received from user {request.user.username}")
        logger.info(f"Files received: {list(request.FILES.keys())}")
        
        form = DjangoProjectForm(request.POST, request.FILES)
        logger.info(f"Form is valid: {form.is_valid()}")
        
        if form.is_valid():
            try:
                # Save the form instance first to get the file
                django_project = form.save(commit=False)
                django_project.user = request.user

                # Generate safe project name with better validation
                import re
                project_name_cleaned = django_project.project_name.strip()
                safe_name = "".join(c if c.isalnum() else "_" for c in project_name_cleaned)
                
                # Remove consecutive underscores and clean up
                safe_name = re.sub(r'_{2,}', '_', safe_name)  # Replace multiple underscores with single
                safe_name = safe_name.strip('_')  # Remove leading/trailing underscores
                
                if len(safe_name) < 3:
                    safe_name = f"{safe_name}_project"
                
                # Make sure it's still valid after cleaning
                if len(safe_name) < 3:
                    safe_name = f"user_project_{uuid.uuid4().hex[:6]}"
                    
                unique_id = uuid.uuid4().hex[:6]
                django_project.subdomain = f"{request.user.username}-{safe_name}-{unique_id}".lower()

                # Save the project to get the file path
                django_project.save()
                
                logger.info(f"Django project saved with ID: {django_project.id}")
                logger.info(f"Project file path: {django_project.project_file.path}")

                # Set project folder path with validation
                WEBSITES_ROOT = getattr(settings, 'WEBSITES_ROOT', 
                                      os.path.join(settings.MEDIA_ROOT, "websites"))
                
                # Ensure WEBSITES_ROOT exists
                os.makedirs(WEBSITES_ROOT, exist_ok=True)
                
                project_folder = os.path.join(WEBSITES_ROOT, f"{request.user.username}_{safe_name}")
                django_project.project_folder = project_folder
                django_project.deployment_status = 'deploying'  # Set initial status
                django_project.save()

                # Deploy the project with detailed error handling
                try:
                    deployment_result = deploy_django_project(
                        request.user.username,
                        safe_name,
                        django_project.project_file.path,
                        django_project.custom_domain
                    )
                    
                    logger.info(f"Deployment result: {deployment_result}")
                    
                    # Handle dictionary return format
                    if deployment_result and isinstance(deployment_result, dict):
                        if deployment_result.get('success'):
                            # Successful deployment
                            domain_name = deployment_result.get('domain_name')
                            django_project.domain_name = domain_name
                            django_project.is_active = True
                            django_project.deployment_status = 'deployed'
                            django_project.save()

                            logger.info(f"Django project deployed successfully: {domain_name}")
                            messages.success(request, f"Django project deployed successfully! Visit: http://{domain_name}")
                            return redirect('django_projects')
                        else:
                            # Deployment failed with specific error
                            error_msg = deployment_result.get('error', 'Unknown deployment error')
                            logger.error(f"Django deployment failed: {error_msg}")
                            django_project.deployment_status = 'failed'
                            django_project.save()
                            
                            # Provide user-friendly error messages
                            if "Not a valid Django project" in error_msg:
                                messages.error(request, "Invalid Django project: Make sure your ZIP file contains manage.py and settings.py files.")
                            elif "Required file not found" in error_msg:
                                messages.error(request, "Missing required files in your Django project. Please check your project structure.")
                            elif "Permission denied" in error_msg:
                                messages.error(request, "Server permission error. Your project may still be deployed but some features might not work. Contact administrator if needed.")
                            elif "Invalid or corrupted ZIP file" in error_msg:
                                messages.error(request, "The uploaded file is invalid or corrupted. Please check your ZIP file and try again.")
                            elif "Docker deployment failed" in error_msg:
                                messages.error(request, "Docker deployment failed. Please check your requirements.txt and project structure.")
                            elif "timeout" in error_msg.lower():
                                messages.error(request, "Deployment timeout. Your project might be too large or complex. Please try again.")
                            else:
                                messages.error(request, f"Deployment failed: {error_msg}")
                    else:
                        # Handle legacy string return or None (old format)
                        if deployment_result and isinstance(deployment_result, str):
                            # Old-style string domain return
                            django_project.domain_name = deployment_result
                            django_project.is_active = True
                            django_project.deployment_status = 'deployed'
                            django_project.save()
                            messages.success(request, f"Django project deployed successfully! Visit: http://{deployment_result}")
                            return redirect('django_projects')
                        else:
                            # None or failed deployment (old format)
                            django_project.deployment_status = 'failed'
                            django_project.save()
                            messages.error(request, "Django project deployment failed. Please check your project structure and requirements.")
                
                except Exception as deploy_error:
                    logger.error(f"Django deployment exception: {str(deploy_error)}")
                    django_project.deployment_status = 'failed'
                    django_project.save()
                    
                    # Provide more specific error messages based on error type
                    error_msg = str(deploy_error)
                    
                    if "No such file or directory" in error_msg:
                        if "/etc/nginx/" in error_msg:
                            messages.warning(request, "Your Django project was deployed successfully but web server configuration was skipped due to permissions. Your app should still be accessible.")
                            # Try to update the project as successful since nginx is optional
                            try:
                                # Check if containers are actually running
                                container_check = subprocess.run([
                                    'docker', 'ps', '--filter', f'name=web_{request.user.username}_{safe_name}', 
                                    '--format', 'table {{.Status}}'
                                ], capture_output=True, text=True, timeout=5)
                                
                                if 'Up' in container_check.stdout:
                                    django_project.is_active = True
                                    django_project.deployment_status = 'deployed'
                                    django_project.domain_name = f"{request.user.username}-{safe_name}.localhost"
                                    django_project.save()
                                    messages.info(request, f"Your Django project is running! Access it at: http://localhost:[container-port]")
                                    return redirect('django_projects')
                            except:
                                pass
                        elif "manage.py" in error_msg:
                            messages.error(request, "Django project structure error: manage.py file not found or inaccessible.")
                        else:
                            messages.error(request, f"File system error: {error_msg}")
                    elif "Permission denied" in error_msg:
                        messages.warning(request, "Permission warnings occurred during deployment. Your project may still work with limited functionality.")
                    elif "docker" in error_msg.lower():
                        if "not found" in error_msg.lower():
                            messages.error(request, "Docker is not available on this server. Please contact system administrator.")
                        elif "timeout" in error_msg.lower():
                            messages.error(request, "Docker deployment timeout. Your project might be too large. Please try again or reduce project size.")
                        else:
                            messages.error(request, "Docker deployment error. Please check your project requirements and try again.")
                    elif "zipfile" in error_msg.lower() or "bad zip" in error_msg.lower():
                        messages.error(request, "Invalid ZIP file format. Please ensure your file is a valid ZIP archive.")
                    elif "timeout" in error_msg.lower():
                        messages.error(request, "Deployment timeout. Please try again with a smaller project or contact support.")
                    else:
                        messages.error(request, f"Deployment error: {error_msg}")

            except Exception as e:
                logger.error(f"Django deployment preparation error for user {request.user.username}: {str(e)}")
                
                # Handle form/file processing errors
                if "project_file" in str(e).lower():
                    messages.error(request, "Error processing uploaded file. Please ensure it's a valid ZIP file and try again.")
                elif "database" in str(e).lower():
                    messages.error(request, "Database error while saving project. Please try again.")
                elif "IntegrityError" in str(e):
                    messages.error(request, "A project with similar details already exists. Please try with a different name.")
                else:
                    messages.error(request, f"Deployment preparation failed: {str(e)}")
        else:
            # Handle form validation errors
            logger.error(f"Form validation errors: {form.errors}")
            
            # Display user-friendly form errors
            for field, errors in form.errors.items():
                for error in errors:
                    if field == 'project_name':
                        if 'at least 3 characters' in str(error):
                            messages.error(request, "Project name must be at least 3 characters long. Please enter a longer name.")
                        elif 'letters, numbers, hyphens, and underscores' in str(error):
                            messages.error(request, "Project name can only contain letters, numbers, spaces, hyphens, and underscores.")
                        elif 'cannot exceed' in str(error):
                            messages.error(request, "Project name is too long. Please use a shorter name (max 50 characters).")
                        else:
                            messages.error(request, f"Project name error: {error}")
                    elif field == 'project_file':
                        if 'ZIP file' in str(error):
                            messages.error(request, "Please upload a valid ZIP file containing your Django project.")
                        elif 'manage.py' in str(error):
                            messages.error(request, "Your Django project must contain a manage.py file in the root or subdirectory.")
                        elif 'settings.py' in str(error):
                            messages.error(request, "Your Django project must contain a settings.py file.")
                        elif 'size' in str(error) or 'exceed' in str(error):
                            messages.error(request, "File size is too large. Maximum allowed size is 100MB. Please reduce your project size.")
                        elif 'corrupted' in str(error):
                            messages.error(request, "The ZIP file appears to be corrupted. Please create a new ZIP file and try again.")
                        else:
                            messages.error(request, f"Project file error: {error}")
                    elif field == 'custom_domain':
                        messages.error(request, "Invalid domain format. Please enter a valid domain name (e.g., example.com).")
                    elif field == 'environment_vars':
                        if 'format' in str(error):
                            messages.error(request, "Environment variables must be in KEY=value format, one per line.")
                        else:
                            messages.error(request, f"Environment variables error: {error}")
                    else:
                        messages.error(request, f"{field.replace('_', ' ').title()}: {error}")
    else:
        form = DjangoProjectForm()

    return render(request, 'deploy_django.html', {'form': form})

@login_required
def toggle_django_project_status(request, project_id):
    """Toggle Django project active/inactive status"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        
        if request.method == 'POST':
            import json
            data = json.loads(request.body)
            new_status = data.get('active', False)
            
            # Update project status
            project.is_active = new_status
            
            if new_status:
                # Activate project - start containers
                safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
                project_folder = project.project_folder
                
                if project_folder and os.path.exists(project_folder):
                    try:
                        original_dir = os.getcwd()
                        os.chdir(project_folder)
                        
                        # Start containers
                        result = subprocess.run(
                            ['docker-compose', 'up', '-d'], 
                            capture_output=True, text=True, timeout=60
                        )
                        
                        if result.returncode == 0:
                            project.deployment_status = 'deployed'
                            message = 'Project activated successfully!'
                        else:
                            project.deployment_status = 'failed'
                            project.is_active = False
                            message = f'Failed to activate project: {result.stderr}'
                            
                        os.chdir(original_dir)
                        
                    except subprocess.TimeoutExpired:
                        project.deployment_status = 'failed'
                        project.is_active = False
                        message = 'Project activation timed out'
                        os.chdir(original_dir)
                    except Exception as e:
                        project.deployment_status = 'failed'
                        project.is_active = False
                        message = f'Error activating project: {str(e)}'
                        try:
                            os.chdir(original_dir)
                        except:
                            pass
                else:
                    project.deployment_status = 'failed'
                    project.is_active = False
                    message = 'Project folder not found'
            else:
                # Deactivate project - stop containers
                safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
                project_folder = project.project_folder
                
                if project_folder and os.path.exists(project_folder):
                    try:
                        original_dir = os.getcwd()
                        os.chdir(project_folder)
                        
                        # Stop containers
                        result = subprocess.run(
                            ['docker-compose', 'down'], 
                            capture_output=True, text=True, timeout=30
                        )
                        
                        project.deployment_status = 'stopped'
                        message = 'Project deactivated successfully!'
                        os.chdir(original_dir)
                        
                    except Exception as e:
                        message = f'Error deactivating project: {str(e)}'
                        try:
                            os.chdir(original_dir)
                        except:
                            pass
                else:
                    project.deployment_status = 'stopped'
                    message = 'Project deactivated (folder not found)'
            
            project.save()
            
            return JsonResponse({
                'success': True,
                'message': message,
                'status': project.deployment_status,
                'is_active': project.is_active
            })
        else:
            return JsonResponse({'success': False, 'error': 'Method not allowed'})
            
    except Exception as e:
        logger.error(f"Toggle status error for project {project_id}: {str(e)}")
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
def django_projects_view(request):
    """List all Django projects"""
    projects = DjangoProject.objects.filter(user=request.user)
    
    # Update status for each project
    for project in projects:
        if project.domain_name and project.is_active:
            safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
            status = check_django_deployment_status(
                request.user.username,
                safe_name,
                project.domain_name
            )
            project.current_status = status
        else:
            project.current_status = {'status': False}
    
    return render(request, 'django_projects.html', {'projects': projects})

@login_required
def django_project_detail(request, project_id):
    """View Django project details"""
    project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
    
    # Get detailed status
    safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
    status = check_django_deployment_status(
        request.user.username,
        safe_name,
        project.domain_name
    ) if project.domain_name else {'status': False}
    
    # Get project info
    project_info = {}
    if project.project_folder and os.path.exists(project.project_folder):
        project_info = get_django_project_info(project.project_folder)
    
    context = {
        'project': project,
        'status': status,
        'project_info': project_info
    }
    
    return render(request, 'django_project_detail.html', context)

@login_required
def delete_django_project(request, project_id):
    """Delete Django project and cleanup resources"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
        
        # Cleanup deployment resources
        cleanup_django_deployment(request.user.username, safe_name)
        
        # Remove project files
        if project.project_folder and os.path.exists(project.project_folder):
            import shutil
            shutil.rmtree(project.project_folder, ignore_errors=True)
        
        # Delete database record
        project.delete()
        
        messages.success(request, "Django project deleted successfully!")
        
    except Exception as e:
        logger.error(f"Django project deletion error: {str(e)}")
        messages.error(request, f"Failed to delete project: {str(e)}")
    
    return redirect('django_projects')

@login_required
def restart_django_project(request, project_id):
    """Restart Django project containers"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
        
        # Restart containers
        project_folder = project.project_folder
        
        if project_folder and os.path.exists(project_folder):
            os.chdir(project_folder)
            import subprocess
            subprocess.run(['docker-compose', 'restart'], capture_output=True)
            messages.success(request, "Django project restarted successfully!")
        else:
            messages.error(request, "Project folder not found!")
            
    except Exception as e:
        logger.error(f"Django project restart error: {str(e)}")
        messages.error(request, f"Failed to restart project: {str(e)}")
    
    return redirect('django_project_detail', project_id=project_id)

@login_required
def django_projects_view(request):
    """List all Django projects with improved status checking"""
    projects = DjangoProject.objects.filter(user=request.user)
    
    # Update status for each project
    for project in projects:
        if project.domain_name and project.deployment_status != 'failed':
            safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
            try:
                status = check_django_deployment_status(
                    request.user.username,
                    safe_name,
                    project.domain_name
                )
                project.current_status = status
                
                # Update database status if needed
                if status.get('status') and not project.is_active:
                    project.is_active = True
                    project.deployment_status = 'deployed'
                    project.save()
                elif not status.get('status') and project.is_active:
                    project.is_active = False
                    project.deployment_status = 'error'
                    project.save()
                    
            except Exception as e:
                logger.error(f"Error checking status for project {project.id}: {e}")
                project.current_status = {'status': False, 'error': str(e)}
        else:
            project.current_status = {'status': False}
    
    return render(request, 'django_projects.html', {'projects': projects})


# Update the django_project_detail view as well
@login_required
def django_project_detail(request, project_id):
    """View Django project details with improved error handling"""
    project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
    
    # Get detailed status
    status = {'status': False}
    if project.domain_name and project.deployment_status != 'failed':
        safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
        try:
            status = check_django_deployment_status(
                request.user.username,
                safe_name,
                project.domain_name
            )
        except Exception as e:
            logger.error(f"Error checking detailed status for project {project_id}: {e}")
            status = {'status': False, 'error': str(e)}
    
    # Get project info
    project_info = {}
    if project.project_folder and os.path.exists(project.project_folder):
        try:
            project_info = get_django_project_info(project.project_folder)
        except Exception as e:
            logger.error(f"Error getting project info for {project_id}: {e}")
            project_info = {'error': str(e)}
    
    context = {
        'project': project,
        'status': status,
        'project_info': project_info
    }
    
    return render(request, 'django_project_detail.html', context)


# Update the restart function
@login_required
def restart_django_project(request, project_id):
    """Restart Django project containers with better error handling"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
        
        # Update project status to restarting
        project.deployment_status = 'restarting'
        project.save()
        
        project_folder = project.project_folder
        
        if project_folder and os.path.exists(project_folder):
            original_dir = os.getcwd()
            try:
                os.chdir(project_folder)
                
                # Restart containers with timeout
                restart_result = subprocess.run(
                    ['docker-compose', 'restart'], 
                    capture_output=True, text=True, timeout=60
                )
                
                if restart_result.returncode == 0:
                    project.deployment_status = 'deployed'
                    project.save()
                    messages.success(request, "Django project restarted successfully!")
                else:
                    project.deployment_status = 'error'
                    project.save()
                    messages.error(request, f"Failed to restart project: {restart_result.stderr}")
                    
            except subprocess.TimeoutExpired:
                project.deployment_status = 'error'
                project.save()
                messages.error(request, "Restart operation timed out. Please try again.")
            except Exception as e:
                project.deployment_status = 'error'
                project.save()
                messages.error(request, f"Error during restart: {str(e)}")
            finally:
                os.chdir(original_dir)
        else:
            project.deployment_status = 'failed'
            project.save()
            messages.error(request, "Project folder not found! Project may need to be redeployed.")
            
    except Exception as e:
        logger.error(f"Django project restart error: {str(e)}")
        messages.error(request, f"Failed to restart project: {str(e)}")
    
    return redirect('django_project_detail', project_id=project_id)

import subprocess
# Update the logs function
@login_required
def django_project_logs(request, project_id):
    """Get Django project logs via AJAX with better error handling"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
        
        # Get logs from both containers
        web_logs = ""
        db_logs = ""
        
        try:
            # Get web container logs
            web_result = subprocess.run([
                'docker', 'logs', '--tail', '100', f'web_{request.user.username}_{safe_name}'
            ], capture_output=True, text=True, timeout=10)
            
            if web_result.returncode == 0:
                web_logs = web_result.stdout + web_result.stderr
            else:
                web_logs = f"Could not retrieve web logs: {web_result.stderr}"
                
        except subprocess.TimeoutExpired:
            web_logs = "Web logs retrieval timed out"
        except Exception as e:
            web_logs = f"Error retrieving web logs: {str(e)}"
        
        try:
            # Get database container logs
            db_result = subprocess.run([
                'docker', 'logs', '--tail', '50', f'db_{request.user.username}_{safe_name}'
            ], capture_output=True, text=True, timeout=10)
            
            if db_result.returncode == 0:
                db_logs = db_result.stdout + db_result.stderr
            else:
                db_logs = f"Could not retrieve database logs: {db_result.stderr}"
                
        except subprocess.TimeoutExpired:
            db_logs = "Database logs retrieval timed out"
        except Exception as e:
            db_logs = f"Error retrieving database logs: {str(e)}"
        
        logs = f"=== Web Container Logs ===\n{web_logs}\n\n=== Database Container Logs ===\n{db_logs}"
        
        return JsonResponse({'logs': logs, 'success': True})
        
    except Exception as e:
        return JsonResponse({'error': str(e), 'success': False})
    
@login_required
def update_django_project(request, project_id):
    """Update Django project with new code"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        
        if request.method == 'POST':
            project_file = request.FILES.get('project_file')
            update_notes = request.POST.get('update_notes', '')
            
            if not project_file:
                return JsonResponse({'success': False, 'error': 'No file provided'})
            
            if not project_file.name.lower().endswith('.zip'):
                return JsonResponse({'success': False, 'error': 'Only ZIP files are allowed'})
            
            # Validate file size (100MB limit)
            if project_file.size > 100 * 1024 * 1024:
                return JsonResponse({'success': False, 'error': 'File size exceeds 100MB limit'})
            
            try:
                # Create backup of current project
                import shutil
                from datetime import datetime
                
                backup_folder = None
                if project.project_folder and os.path.exists(project.project_folder):
                    backup_folder = f"{project.project_folder}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    shutil.copytree(project.project_folder, backup_folder)
                
                # Save new file
                old_file_path = project.project_file.path if project.project_file else None
                project.project_file = project_file
                project.save()
                
                # Clean project name for deployment
                safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
                
                # Stop current containers
                if project.project_folder and os.path.exists(project.project_folder):
                    try:
                        original_dir = os.getcwd()
                        os.chdir(project.project_folder)
                        subprocess.run(['docker-compose', 'down'], capture_output=True, timeout=30)
                        os.chdir(original_dir)
                    except:
                        pass
                
                # Redeploy with new code
                deployment_result = deploy_django_project(
                    request.user.username,
                    safe_name,
                    project.project_file.path,
                    project.custom_domain
                )
                
                if deployment_result and isinstance(deployment_result, dict) and deployment_result.get('success'):
                    project.deployment_status = 'deployed'
                    project.is_active = True
                    
                    # Clean up old file
                    if old_file_path and os.path.exists(old_file_path):
                        try:
                            os.remove(old_file_path)
                        except:
                            pass
                    
                    # Clean up backup if deployment successful
                    if backup_folder and os.path.exists(backup_folder):
                        try:
                            shutil.rmtree(backup_folder)
                        except:
                            pass
                    
                    project.save()
                    
                    return JsonResponse({
                        'success': True,
                        'message': 'Project updated successfully!',
                        'domain': deployment_result.get('domain_name')
                    })
                else:
                    # Deployment failed - restore backup
                    project.deployment_status = 'failed'
                    project.is_active = False
                    
                    if backup_folder and os.path.exists(backup_folder):
                        try:
                            if project.project_folder and os.path.exists(project.project_folder):
                                shutil.rmtree(project.project_folder)
                            shutil.move(backup_folder, project.project_folder)
                            
                            # Restart old containers
                            original_dir = os.getcwd()
                            os.chdir(project.project_folder)
                            subprocess.run(['docker-compose', 'up', '-d'], capture_output=True, timeout=60)
                            os.chdir(original_dir)
                            
                            project.deployment_status = 'deployed'
                            project.is_active = True
                        except Exception as restore_error:
                            logger.error(f"Failed to restore backup: {restore_error}")
                    
                    project.save()
                    
                    error_msg = deployment_result.get('error', 'Deployment failed') if isinstance(deployment_result, dict) else 'Deployment failed'
                    return JsonResponse({
                        'success': False,
                        'error': f'Update failed: {error_msg}. Original version restored.'
                    })
                    
            except Exception as e:
                logger.error(f"Project update error: {str(e)}")
                return JsonResponse({'success': False, 'error': f'Update error: {str(e)}'})
        else:
            return JsonResponse({'success': False, 'error': 'Method not allowed'})
            
    except Exception as e:
        logger.error(f"Update project error for project {project_id}: {str(e)}")
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
def django_project_metrics(request, project_id):
    """Get Django project metrics and usage stats"""
    try:
        project = get_object_or_404(DjangoProject, id=project_id, user=request.user)
        safe_name = "".join(c if c.isalnum() else "_" for c in project.project_name)
        
        metrics = {
            'cpu_usage': 0,
            'memory_usage': 0,
            'disk_usage': 0,
            'requests_count': 0,
            'uptime': 0,
            'status': 'unknown'
        }
        
        try:
            # Get container stats if available
            container_name = f"web_{request.user.username}_{safe_name}"
            
            # Check if container is running
            check_result = subprocess.run([
                'docker', 'ps', '--filter', f'name={container_name}', '--format', 'table {{.Names}}\t{{.Status}}'
            ], capture_output=True, text=True, timeout=10)
            
            if container_name in check_result.stdout:
                metrics['status'] = 'running'
                
                # Get detailed stats
                stats_result = subprocess.run([
                    'docker', 'stats', container_name, '--no-stream', '--format',
                    'table {{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}'
                ], capture_output=True, text=True, timeout=10)
                
                if stats_result.returncode == 0 and stats_result.stdout:
                    lines = stats_result.stdout.strip().split('\n')
                    if len(lines) > 1:  # Skip header
                        data = lines[1].split('\t')
                        if len(data) >= 3:
                            metrics['cpu_usage'] = float(data[0].replace('%', ''))
                            memory_parts = data[1].split('/')
                            if len(memory_parts) >= 2:
                                used = memory_parts[0].strip()
                                total = memory_parts[1].strip()
                                metrics['memory_usage'] = f"{used} / {total}"
                            metrics['memory_percent'] = float(data[2].replace('%', ''))
            else:
                metrics['status'] = 'stopped'
                
            # Get disk usage if project folder exists
            if project.project_folder and os.path.exists(project.project_folder):
                def get_dir_size(path):
                    total_size = 0
                    for dirpath, dirnames, filenames in os.walk(path):
                        for filename in filenames:
                            filepath = os.path.join(dirpath, filename)
                            try:
                                total_size += os.path.getsize(filepath)
                            except (OSError, IOError):
                                pass
                    return total_size
                
                size_bytes = get_dir_size(project.project_folder)
                size_mb = round(size_bytes / (1024 * 1024), 2)
                metrics['disk_usage'] = f"{size_mb} MB"
            
        except subprocess.TimeoutExpired:
            metrics['status'] = 'timeout'
        except Exception as e:
            logger.error(f"Error getting metrics for project {project_id}: {e}")
            metrics['status'] = 'error'
        
        return JsonResponse({'success': True, 'metrics': metrics})
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
    


# Static Website Management (existing functionality)
@login_required
def deploy_static_view(request):
    """Deploy static website"""
    domain_link = None
    
    if request.method == 'POST':
        form = WebsiteForm(request.POST, request.FILES)
        if form.is_valid():
            website = form.save(commit=False)
            website.user = request.user

            # Clean title for URL/folder
            safe_title = "".join(c if c.isalnum() else "_" for c in website.title)
            unique_id = uuid.uuid4().hex[:6]
            website.subdomain = f"{request.user.username}-{safe_title}-{unique_id}".lower()

            # Set folder path
            WEBSITES_ROOT = getattr(settings, 'WEBSITES_ROOT', 
                                  os.path.join(settings.BASE_DIR, "media", "websites"))
            website.folder_name = os.path.join(WEBSITES_ROOT, f"{request.user.username}_{safe_title}")
            
            website.save()

            # Deploy site using existing static deployment logic
            from .utils import deploy_website
            domain_link = deploy_website(
                request.user.username,
                safe_title,
                website.uploaded_file.path,
                is_dynamic=False,
                custom_domain=website.custom_domain
            )

            if domain_link:
                website.domain_name = domain_link
                website.is_active = True
                website.save()
                messages.success(request, f"Static website deployed! Visit: {domain_link}")
            else:
                messages.error(request, "Static website deployment failed.")
    else:
        form = WebsiteForm()

    return render(request, 'deploy_static.html', {'form': form, 'domain_link': domain_link})

@login_required
def websites(request):
    """List all static websites"""
    user_websites = Website.objects.filter(user=request.user)
    
    # Check status of each website
    for website in user_websites:
        if website.domain_name:
            from .utils import check_deployment_status
            website.status = check_deployment_status(
                request.user.username, 
                website.title, 
                website.domain_name
            )
        else:
            website.status = False
    
    return render(request, 'websites.html', {'websites': user_websites})

@login_required
def delete_website(request, website_id):
    """Delete static website"""
    try:
        website = Website.objects.get(id=website_id, user=request.user)
        
        # Clean up deployment resources
        from .utils import cleanup_deployment
        safe_title = "".join(c if c.isalnum() else "_" for c in website.title)
        cleanup_deployment(request.user.username, safe_title)
        
        # Remove files
        if website.folder_name and os.path.exists(website.folder_name):
            import shutil
            shutil.rmtree(website.folder_name)
        
        website.delete()
        messages.success(request, "Website deleted successfully!")
        
    except Website.DoesNotExist:
        messages.error(request, "Website not found!")
    except Exception as e:
        logger.error(f"Website deletion error: {str(e)}")
        messages.error(request, "Failed to delete website!")
    
    return redirect('websites')

@login_required
def settings_view(request):
    context = {
        'total_deployments': Website.objects.filter(user=request.user).count() + DjangoProject.objects.filter(user=request.user).count(),
        'active_sites': Website.objects.filter(user=request.user, is_active=True).count() + DjangoProject.objects.filter(user=request.user, is_active=True).count(),
        'storage_used': 0,  # Calculate actual storage
        'bandwidth_used': 0,  # Calculate actual bandwidth
    }
    
    if request.method == 'POST':
        form_type = request.POST.get('form_type')
        
        if form_type == 'profile':
            # Handle profile update
            user = request.user
            user.email = request.POST.get('email')
            user.first_name = request.POST.get('first_name')
            user.last_name = request.POST.get('last_name')
            user.save()
            messages.success(request, 'Profile updated successfully!')
            
        elif form_type == 'password':
            # Handle password change
            # Add your password change logic here
            pass
            
        elif form_type == 'deployment':
            # Handle deployment preferences
            # Add your preferences logic here
            pass
    
    return render(request, 'settings.html', context)


from django.db.models import Count, Q
from django.utils import timezone
import json
from datetime import datetime, timedelta
@login_required
def reports(request):
    """Comprehensive reports and analytics view"""
    user = request.user
    
    # Get all user's projects
    websites = Website.objects.filter(user=user)
    django_projects = DjangoProject.objects.filter(user=user)
    
    # Basic stats
    total_static_sites = websites.count()
    total_django_projects = django_projects.count()
    total_deployments = total_static_sites + total_django_projects
    
    active_static_sites = websites.filter(is_active=True).count()
    active_django_projects = django_projects.filter(is_active=True).count()
    active_sites = active_static_sites + active_django_projects
    
    # Calculate success rate (percentage of active vs total)
    success_rate = round((active_sites / total_deployments * 100) if total_deployments > 0 else 0, 1)
    
    # Calculate storage usage (in MB)
    storage_used = 0
    for website in websites:
        try:
            if website.uploaded_file and hasattr(website.uploaded_file, 'size'):
                storage_used += website.uploaded_file.size
        except:
            pass
    
    for project in django_projects:
        try:
            if project.project_file and hasattr(project.project_file, 'size'):
                storage_used += project.project_file.size
        except:
            pass
    
    storage_used = round(storage_used / (1024 * 1024), 2)  # Convert to MB
    
    # Generate deployment timeline data (last 30 days)
    end_date = timezone.now().date()
    start_date = end_date - timedelta(days=30)
    
    deployment_dates = []
    deployment_counts = []
    
    # Create date range for the last 30 days
    current_date = start_date
    while current_date <= end_date:
        deployment_dates.append(current_date.strftime('%m/%d'))
        
        # Count deployments for this date
        daily_websites = websites.filter(created_at__date=current_date).count()
        daily_django = django_projects.filter(created_at__date=current_date).count()
        daily_total = daily_websites + daily_django
        
        deployment_counts.append(daily_total)
        current_date += timedelta(days=1)
    
    # Recent deployments (last 10)
    recent_websites = list(websites.order_by('-created_at')[:5])
    recent_django = list(django_projects.order_by('-created_at')[:5])
    
    # Combine and sort recent deployments
    recent_deployments = recent_websites + recent_django
    recent_deployments.sort(key=lambda x: x.created_at, reverse=True)
    recent_deployments = recent_deployments[:10]
    
    # All projects for the table (combine both types)
    all_projects = []
    
    # Add websites
    for website in websites.order_by('-created_at'):
        all_projects.append({
            'title': website.title,
            'project_name': None,  # This helps distinguish in template
            'is_active': website.is_active,
            'domain_name': website.domain_name,
            'created_at': website.created_at,
            'project_file': None,
            'uploaded_file': website.uploaded_file
        })
    
    # Add Django projects
    for project in django_projects.order_by('-created_at'):
        all_projects.append({
            'title': None,
            'project_name': project.project_name,
            'is_active': project.is_active,
            'domain_name': project.domain_name,
            'created_at': project.created_at,
            'project_file': project.project_file,
            'uploaded_file': None
        })
    
    # Sort all projects by creation date
    all_projects.sort(key=lambda x: x['created_at'], reverse=True)
    
    # Convert objects to template-friendly format
    all_projects_formatted = []
    for project in all_projects:
        project_data = type('obj', (object,), project)
        all_projects_formatted.append(project_data)
    
    context = {
        # Basic stats
        'total_deployments': total_deployments,
        'total_static_sites': total_static_sites,
        'total_django_projects': total_django_projects,
        'active_sites': active_sites,
        'active_static_sites': active_static_sites,
        'active_django_projects': active_django_projects,
        'storage_used': storage_used,
        'success_rate': success_rate,
        
        # Chart data
        'deployment_dates': json.dumps(deployment_dates),
        'deployment_counts': json.dumps(deployment_counts),
        'django_count': total_django_projects,
        'static_count': total_static_sites,
        
        # Lists
        'recent_deployments': recent_deployments,
        'all_projects': all_projects_formatted,
        
        # Additional stats
        'websites': websites,
        'django_projects': django_projects,
    }
    
    return render(request, 'reports.html', context)