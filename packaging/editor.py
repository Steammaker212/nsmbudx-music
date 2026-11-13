# Entry point for the nsmbudx-music.exe build: the Studio editor.
import multiprocessing

from nsmbudx_music.studio import main

if __name__ == "__main__":
    multiprocessing.freeze_support()   # the encoder's worker processes re-run this exe
    main()
