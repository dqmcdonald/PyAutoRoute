TEXFILE  = tutorial
PDFFILE  = $(TEXFILE).pdf
LATEX    = pdflatex
LATEXOPT = -interaction=nonstopmode -halt-on-error

.PHONY: all pdf clean

all: pdf

pdf: $(PDFFILE)

# Two passes so that cross-references (TOC, figure numbers) resolve correctly.
$(PDFFILE): $(TEXFILE).tex images/Schematic.png \
            images/Initial\ PCB\ Layout.png images/Border.png
	$(LATEX) $(LATEXOPT) $(TEXFILE).tex
	$(LATEX) $(LATEXOPT) $(TEXFILE).tex

clean:
	rm -f $(TEXFILE).aux $(TEXFILE).log $(TEXFILE).out \
	      $(TEXFILE).toc $(TEXFILE).pdf
