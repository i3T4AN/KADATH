#!/usr/bin/env perl
use strict;
use warnings;
use utf8;

binmode STDIN,  ':encoding(UTF-8)';
binmode STDOUT, ':encoding(UTF-8)';

my $purple = "\e[38;5;135m";
my $green  = "\e[38;5;84m";
my $blue   = "\e[38;5;39m";
my $shadow = "\e[38;5;238m";
my $reset  = "\e[0m";
my @eye_half_width = (0, 0, 0, 0, 1, 3, 4, 6, 7, 8, 10, 11);

my @lines = <>;
chomp @lines;
my @art = map { [split //] } @lines;
my %seen;
my %enclosed_block;
my %portal_opening;
my @portal_opening_cells;

# Full blocks form both the black background and the solid interiors of the
# artwork. Edge-connected blocks are background; enclosed components belong
# to drawn objects. The large central component is the portal's black opening.
for my $row_index (0 .. $#art) {
    for my $column_index (0 .. $#{$art[$row_index]}) {
        my $origin = "$row_index,$column_index";
        next if $art[$row_index][$column_index] ne '█' || $seen{$origin};

        my @queue = ([$row_index, $column_index]);
        my @component;
        my $touches_edge = 0;
        my $is_opening = 0;
        $seen{$origin} = 1;
        while (@queue) {
            my ($current_row, $current_column) = @{shift @queue};
            push @component, [$current_row, $current_column];
            $touches_edge = 1 if
                $current_row == 0 || $current_row == $#art ||
                $current_column == 0 || $current_column == $#{$art[$current_row]};
            $is_opening = 1 if $current_row == 24 && $current_column == 49;

            for my $neighbor (
                [$current_row - 1, $current_column],
                [$current_row + 1, $current_column],
                [$current_row, $current_column - 1],
                [$current_row, $current_column + 1],
            ) {
                my ($next_row, $next_column) = @$neighbor;
                next if $next_row < 0 || $next_row > $#art;
                next if $next_column < 0 || $next_column > $#{$art[$next_row]};
                my $key = "$next_row,$next_column";
                next if $seen{$key} || $art[$next_row][$next_column] ne '█';
                $seen{$key} = 1;
                push @queue, [$next_row, $next_column];
            }
        }

        if ($is_opening) {
            $portal_opening{"$_->[0],$_->[1]"} = 1 for @component;
            push @portal_opening_cells, @component;
        } elsif (!$touches_edge) {
            $enclosed_block{"$_->[0],$_->[1]"} = 1 for @component;
        }
    }
}

for my $row_index (0 .. $#art) {
    my $row = $row_index + 1;
    my @cells = @{$art[$row_index]};
    my $active = '';

    for my $index (0 .. $#cells) {
        my $column = $index + 1;
        my $cell = $cells[$index];
        my $key = "$row_index,$index";
        my $linework = $cell ne '█' && $cell ne ' ';
        my $object = $linework || $enclosed_block{$key};

        my $eye_half_width = $eye_half_width[$row] // -1;
        my $eye = $row >= 2 && $row <= 11 && $cell ne ' ' &&
            $column >= 51 - $eye_half_width &&
            $column <= 51 + $eye_half_width;

        my $stairs = $row >= 33 && $row <= 42 && $cell ne ' ' &&
            $column >= 43 - (2 * ($row - 33)) &&
            $column <= 58 + (2 * ($row - 33));

        my $ruins = $object && $row >= 30 && $row <= 42 && (
            ($column >= 15 && $column <= 40) ||
            ($column >= 61 && $column <= 86)
        );
        my $ground = $object && $row >= 39;

        my $portal = 0;
        if ($object && !$portal_opening{$key}) {
            for my $opening_cell (@portal_opening_cells) {
                my $horizontal = $index - $opening_cell->[1];
                my $vertical = $row_index - $opening_cell->[0];
                if (($horizontal ** 2) + (6.25 * ($vertical ** 2)) <= 144) {
                    $portal = 1;
                    last;
                }
            }
        }

        # The portal's lower U-rim stays visible over the ruins. Only the
        # central staircase physically occludes it in the reference image.
        my $color = ($eye || $stairs) ? $blue
                  : $portal ? $purple
                  : ($ruins || $ground) ? $blue
                  : $object ? $green
                  : $shadow;
        if ($color ne $active) {
            print $color;
            $active = $color;
        }
        print $cell;
    }
    print "$reset\n";
}
