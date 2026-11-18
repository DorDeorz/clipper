# Altyazı fontu

Altyazılar **Montserrat ExtraBold** ile çizilir (ROADMAP 4.6). Font dosyası repoya konmuyor
(binary); bir kez indirip bu klasöre koy:

1. https://fonts.google.com/specimen/Montserrat adresinden "Download family" ile indir
   (SIL Open Font License).
2. Zip içindeki `static/Montserrat-ExtraBold.ttf` dosyasını bu klasöre
   (`assets/fonts/Montserrat-ExtraBold.ttf`) kopyala.

Font yoksa render yine çalışır; libass sistemdeki bir fontu kullanır. `clipper doctor`
fontun bulunup bulunmadığını gösterir. Klasör `paths.fonts_dir` ile değiştirilebilir.
