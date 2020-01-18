#! /usr/bin/python
#
# Note that the above interpreter choice is correct -
# '/usr/bin/python'. It isn't '/usr/bin/python2' or
# '/usr/bin/python3'. But, this is OK. This script is run in a
# container, and we won't know which python we're executing (or even
# which will be installed), but we should be assured of one or the
# other. This script was written to be executable by either version of
# python (checked by running pylint-2 and pylint-3 on it).

"""Install specific versions of packages."""

# Here we want to make sure that we've got all the right versions of
# certain software installed.

from __future__ import print_function
import os
import os.path
import sys
import subprocess
import re
import platform
import getopt
import shutil

def which(cmd):
    """Find the full path of a command."""
    for path in os.environ["PATH"].split(os.pathsep):
        if os.path.exists(os.path.join(path, cmd)):
            return os.path.join(path, cmd)

    return None

def _eprint(*args, **kwargs):
    """Print to stderr."""
    print(*args, file=sys.stderr, **kwargs)

class PkgSystem(object):
    "A class to hide the details of package management."
    __verbose = 0
    __distro_id = None
    __release = None
    __pkgr_path = None
    __pkgmgr_path = None
    __wget_path = None
    __local_repo_path = ''
    __local_rpm_dir = ''

    def __init__(self, verbose):
        __verbose = verbose

        #
        # Try to figure out what distro/release we've got. We might
        # have to do more later if necessary to figure out the
        # distro/release (like looking at /etc/redhat-release).
        #
        # Note that it isn't an error if we can't figure out the
        # distro/release - we may not need that information.
        lsb_release = which("lsb_release")
        if lsb_release != None:
            try:
                self.__distro_id = subprocess.check_output([lsb_release, "-is"])
                self.__distro_id = self.__distro_id.strip()
            except subprocess.CalledProcessError:
                pass
            try:
                self.__release = subprocess.check_output([lsb_release, "-rs"])
                self.__release = self.__release.strip()
            except subprocess.CalledProcessError:
                pass
        if self.__distro_id is None:
            self.__distro_id = platform.linux_distribution()[0]
        if self.__release is None:
            self.__release = platform.linux_distribution()[1]

        #
        # Make sure we know the base package manager the system uses.
        self.__pkgr_path = which("rpm")
        if self.__pkgr_path is None:
            _eprint("Can't find the 'rpm' executable.")
            sys.exit(1)

        #
        # Find the package manager for this system.
        self.__pkgmgr_path = which("dnf")
        self.__debuginfo_install = [self.__pkgmgr_path, 'debuginfo-install']
        if self.__pkgmgr_path is None:
            self.__pkgmgr_path = which("yum")
            self.__debuginfo_install = [which('debuginfo-install')]
        if self.__pkgmgr_path is None:
            _eprint("Can't find a package manager (either 'dnr' or 'yum').")
            sys.exit(1)

        #
        # See if we've got 'wget'. It isn't an error if we don't have
        # it since we may not need it.
        self.__wget_path = which("wget")

    def build_id_is_valid(self, name, build_id):
        """Return true if the 'name' matches the build id."""
        # First, make sure the build id symbolic link exists. This has
        # to be an exact match.
        build_id_path = '/usr/lib/debug/.build-id/' + build_id[:2] \
                        + '/' + build_id[2:]
        if not os.path.exists(build_id_path):
            if self.__verbose:
                print("Build id %s doesn't exist." % build_id)
            return 0

        # Now we know the build id exists. But, does it point to the
        # correct file? Note that we're comparing basenames here. Why?
        # (1) The kernel doesn't really have a "path". (2)
        # "/usr/bin/FOO" is really the same file as "/bin/FOO"
        # (UsrMove feature).
        sym_target = os.path.basename(os.readlink(build_id_path))
        if name == 'kernel':
            name = 'vmlinux'
        else:
            name = os.path.basename(name)
        if sym_target != name:
            if self.__verbose:
                print("Build id %s doesn't match '%s'." % (build_id, name))
            return 0
        return 1

    def pkg_exists(self, name, pkg_nvr, build_id):
        """Return true if the package and its debuginfo exists."""
        if subprocess.call([self.__pkgr_path, "-qi", pkg_nvr]) != 0:
            return 0
        if not build_id:
            if self.__verbose:
                print("Package %s already exists on the system." % pkg_nvr)
            return 1

        return self.build_id_is_valid(name, build_id)

    def pkg_install(self, name, pkg_nvr, build_id):
        """Install a package and its debuginfo."""
        if subprocess.call([self.__pkgmgr_path, 'install', '-y',
                            pkg_nvr]) != 0:
            return 0
        if not build_id:
            if self.__verbose:
                print("Package %s installed." % pkg_nvr)
            return 1
        if subprocess.call(self.__debuginfo_install + ['-y', pkg_nvr]) != 0:
            return 0

        # OK, at this point we know the package and its debuginfo has
        # been installed. Validiate the build id.
        return self.build_id_is_valid(name, build_id)

    def pkg_download_and_install(self, name, pkg_nvr, build_id):
        """Manually download and install a package."""
        # If we're not on Fedora, we don't know how to get the
        # package.
        if self.__wget_path is None or self.__distro_id is None \
           or self.__distro_id.lower() != "fedora":
            _eprint("Can't download package '%s'" % pkg_nvr)
            return 0

        # Try downloading the package from koji, Fedora's build system.
        nvra_regexp = re.compile(r'^(\w+)-([^-]+)-([^-]+)\.(\w+)$')
        match = nvra_regexp.match(pkg_nvr)
        if not match:
            _eprint("Can't parse package nvr '%s'" % pkg_nvr)
            return 0

        pkg_name = match.group(1)
        pkg_version = match.group(2)
        pkg_release = match.group(3)
        pkg_arch = match.group(4)

        # Build up the koji url. Koji urls look like:
        # http://kojipkgs.fedoraproject.org/packages/NAME/VER/RELEASE/ARCH/
        koji_url = ("http://kojipkgs.fedoraproject.org/packages/%s/%s/%s/%s"
                    % (pkg_name, pkg_version, pkg_release, pkg_arch))
        _eprint("URL: '%s'" % koji_url)

        # Download the entire arch directory. Here's a description of
        # wget's arguments:
        #
        #   --quiet: Don't display progress.
        #   -nH: No host directories (i.e. get rid of the host name in
        #        the download directory name).
        #   --cut-dirs=4: Ignore 4 directory components.
        #   -r: Turn on recursive retrieving.
        #   -l 1: Maximum recursion depth is 1.
        #
        if subprocess.call(['wget', '--quiet', '-nH', '--cut-dirs=4',
                            '-r', '-l', '1', koji_url]) != 0:
            _eprint("Can't download package '%s'" % pkg_nvr)
            return 0

        # OK, now we've got a directory which contains all the RPMs
        # for package 'foo'. We can't just do a "dnf install RPM",
        # because (for example) the 'kernel' RPM requires the
        # 'kernel-core' and 'kernel-firmware' RPMs. We might be able
        # to install all the RPMs we just downloaded, but besides
        # being overkill, it is theoretically possible that they might
        # conflict somehow.
        #
        # So, instead we'll create a local repo that then yum/dnf can
        # use when looking for RPMs.

        # First create the repo file.
        self.__local_repo_path = '/etc/yum.repos.d/local.repo'
        self.__local_rpm_dir = '/root/%s' % pkg_arch
        if not os.path.exists(self.__local_repo_path):
            repo_file = open(self.__local_repo_path, 'w')
            repo_file.write('[local]\n')
            repo_file.write('name=Local repository\n')
            repo_file.write('baseurl=file://%s\n' % self.__local_rpm_dir)
            repo_file.write('enabled=1\n')
            repo_file.write('gpgcheck=0\n')
            repo_file.write('type=rpm\n')
            repo_file.close()

        # Next run 'createrepo_c' on the directory.
        if subprocess.call(['createrepo_c', '--quiet', pkg_arch]) != 0:
            _eprint("Can't run createrepo_c")
            return 0

        # At this point we should be set up to let the package manager
        # install the package.
        return self.pkg_install(name, pkg_nvr, build_id)

    def cleanup(self):
        """Perform cleanup (if necessary)."""
        if self.__local_repo_path:
            os.remove(self.__local_repo_path)
        if self.__local_rpm_dir:
            shutil.rmtree(self.__local_rpm_dir)

def _usage():
    """Display command-line usage."""
    _eprint("Usage: %s [-v] --name NAME --pkg PACKAGE --build_id BUILD_ID"
            % sys.argv[0])
    sys.exit(1)

def _handle_command_line():
    """Process command line."""
    verbose = 0
    name = ''
    pkg_nvr = ''
    build_id = ''

    # Make sure the command line looks reasonable.
    if len(sys.argv) < 4:
        _usage()
    try:
        (opts, pargs) = getopt.getopt(sys.argv[1:], 'v', ['name=', 'pkg=', 'build_id='])
    except getopt.GetoptError as err:
        _eprint("Error: %s" % err)
        _usage()
    for (opt, value) in opts:
        if opt == '-v':
            verbose += 1
        elif opt == '--name':
            name = value
        elif opt == '--pkg':
            pkg_nvr = value
        elif opt == '--build_id':
            build_id = value
    if pargs:
        _usage()
    if not name or not pkg_nvr or not build_id:
        _eprint("Error: '--name', '--pkg', and '--build_id' are required arguments.")
        _usage()
    return (verbose, name, pkg_nvr, build_id)

def main():
    """Main function."""
    (verbose, name, pkg_nvr, build_id) = _handle_command_line()

    # Make sure we're in /root.
    os.chdir('/root')

    packages = []
    packages.append([name, pkg_nvr, build_id])

    # If the package name is 'kernel', we've got to do some special
    # processing. We also want to install the matching kernel-devel
    # (along with the debuginfo).
    #
    # Note that we have to handle/recognize kernel variants, like
    # 'kernel-PAE' or 'kernel-debug'.
    kernel_regexp = re.compile(r'^kernel(-\w+)?')
    match = kernel_regexp.match(name)
    if match:
        devel_name = name + '-devel'
        devel_nvr = re.sub(name, devel_name, pkg_nvr)
        packages.append([devel_name, devel_nvr, ''])

    pkgsys = PkgSystem(verbose)
    for (name, pkg_nvr, build_id) in packages:
        # Is the correct package version already installed?
        if pkgsys.pkg_exists(name, pkg_nvr, build_id):
            continue

        # Try using the package manager to install the package
        if pkgsys.pkg_install(name, pkg_nvr, build_id):
            continue

        # As a last resort, try downloading and installing the package
        # manually.
        if pkgsys.pkg_download_and_install(name, pkg_nvr, build_id):
            continue

        _eprint("Can't find package '%s'" % pkg_nvr)
        sys.exit(1)

    if verbose:
        print("All packages installed.")

    # Perform cleanup, if needed.
    pkgsys.cleanup()

    sys.exit(0)

if __name__ == '__main__':
    main()
