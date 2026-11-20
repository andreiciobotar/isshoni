# Records assets/tabs/factory.gif: the Factory floor over invented repositories, cubes moving, then the `/` filter.
# Run it through tools/floor.sh from the repository root: that sets BIN, FLOOR_REPOS and ISSHONI_ROOT.
Output assets/tabs/factory.gif
Set Shell bash
Set FontSize 14
Set Width 1200
Set Height 640
Set Padding 8
Set Framerate 8
Set TypingSpeed 0

Hide
Type "export PS1='$ ' && cd $FLOOR_REPOS && clear && $BIN --projects-dir $FLOOR_REPOS --range all"
Enter
Sleep 2s
Type "4"
Show
Sleep 24s
Type "/"
Sleep 2s
Down
Sleep 1s
Down
Sleep 1s
Enter
Sleep 3s
