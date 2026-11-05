# Entry point for the nsmbudx-music.exe editor build.
import multiprocessing

from nsmbudx_music.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()   # the encoder's worker processes re-run this exe
    main()
