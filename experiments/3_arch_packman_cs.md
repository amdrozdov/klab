### Update package list
sudo pacman -Syy

### Update and upgrade all	
sudo pacman -Syu	

### Install specific package	
sudo pacman -S pkgname

### Find available packages
sudo pacman -Ss keyword	

### Find available local packages
sudo pacman -Qs keyword

### List all files from package	
pacman -Ql pkgname

### Pacman log file
/var/log/pacman.log

### Screen recording
gtk-recordmydesktop

### Mount iso file
fuseiso -p  testimage.iso testimagemountpoint

### To unmount
fusermount -u <mountpoint>
  
### To remove a package and its dependencies which are not required by any other installed package
sudo pacman -Rs package_name

### List all packages no longer required as dependencies
sudo pacman -Qdt

### Get IP address
ip addr

### Update and upgrade yaourt packages
yaourt -Syua

### Get distro version
lsb_release -a